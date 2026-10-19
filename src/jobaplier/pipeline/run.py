"""Sökkörningen: hämta, deduplicera, filtrera och poängsätt."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine
from sqlmodel import Session, select

from jobaplier.ai import AIClient, AIError, BudgetExceeded
from jobaplier.config import Preferences
from jobaplier.db import session_scope
from jobaplier.models import ApplyMethod, Job, JobStatus, Run, utcnow
from jobaplier.pipeline.dedupe import upsert_raw_jobs
from jobaplier.pipeline.filter import hard_filter
from jobaplier.pipeline.scoring import score_job
from jobaplier.profile.cv import CVMissing, CVUnreadable, Profile, load_or_build_profile, profile_of
from jobaplier.sources.base import Source, SourceError
from jobaplier.status import transition

FIRST_RUN_LOOKBACK = timedelta(days=14)

_run_lock = threading.Lock()
# En hel adress i löptext: avgränsare och en avslutande punkt ingår inte.
_ADDRESS_RE = re.compile(
    r"[^@\s<>()\[\],;:\"']+@[^@\s<>()\[\],;:\"']+\.[^@\s<>()\[\],;:\"'.]+"
)


class RunInProgress(Exception):
    """En körning pågår redan."""


def run_pipeline(
    engine: Engine,
    sources: list[Source],
    prefs: Preferences,
    ai: AIClient,
    cv_path: Path,
    *,
    trigger: Literal["schedule", "manual"],
    now: Callable[[], datetime] = utcnow,
) -> Run:
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("En körning pågår redan")
    try:
        return _run(engine, sources, prefs, ai, cv_path, trigger, now)
    finally:
        _run_lock.release()


def _run(
    engine: Engine,
    sources: list[Source],
    prefs: Preferences,
    ai: AIClient,
    cv_path: Path,
    trigger: str,
    now: Callable[[], datetime],
) -> Run:
    # Run-raden committas först så att en krasch ändå lämnar ett spår.
    with session_scope(engine) as session:
        run = Run(started_at=now(), trigger=trigger)
        session.add(run)
        session.flush()
        run_id = run.id
    assert run_id is not None

    stats: dict[str, Any] = {
        "sources": {},
        "new": 0,
        "filtered": 0,
        "scored": 0,
        "scoring_failed": 0,
    }
    errors: dict[str, str] = {}
    spent = [0.0]  # delad med poängsättningen så att kostnaden överlever en krasch
    try:
        profile = _load_profile(engine, ai, cv_path, errors)
        if profile is not None:
            for source in sources:
                _collect(engine, source, prefs, run_id, now, stats, errors)
            _score_new_jobs(engine, profile, prefs, ai, stats, errors, spent)
    finally:
        # Körs även vid oväntat undantag så att körningen får finished_at.
        with session_scope(engine) as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.stats_json = stats
            run.errors_json = errors
            run.ai_cost_usd = spent[0]
            run.finished_at = now()
        with Session(engine) as session:
            run = session.get(Run, run_id)
            assert run is not None
            session.expunge(run)  # laddad och frånkopplad: fälten går att läsa
    return run


def _load_profile(
    engine: Engine, ai: AIClient, cv_path: Path, errors: dict[str, str]
) -> Profile | None:
    try:
        with session_scope(engine) as session:
            return profile_of(load_or_build_profile(session, cv_path, ai))
    except (CVMissing, CVUnreadable) as exc:
        errors["profile"] = str(exc)
    except BudgetExceeded as exc:
        errors["budget"] = str(exc)
    except AIError as exc:
        errors["profile"] = str(exc)
    return None


def _since(session: Session, run_id: int, name: str, now: Callable[[], datetime]) -> datetime:
    earlier = session.exec(
        select(Run)
        .where(Run.id != run_id)
        .order_by(Run.started_at.desc(), Run.id.desc())  # type: ignore[union-attr]
    )
    for previous in earlier:
        entry = (previous.stats_json.get("sources") or {}).get(name)
        if entry and entry.get("ok"):
            return previous.started_at
    return now() - FIRST_RUN_LOOKBACK


def _collect(
    engine: Engine,
    source: Source,
    prefs: Preferences,
    run_id: int,
    now: Callable[[], datetime],
    stats: dict[str, Any],
    errors: dict[str, str],
) -> None:
    name = source.name
    try:
        with session_scope(engine) as session:
            since = _since(session, run_id, name, now)
            raws = source.fetch(since)
            created = upsert_raw_jobs(session, raws)
            filtered = 0
            for job in created:
                reason = hard_filter(job, prefs)
                if reason is not None:
                    job.filter_reason = reason
                    transition(job, JobStatus.filtered)
                    filtered += 1
        stats["new"] += len(created)
        stats["filtered"] += filtered
        stats["sources"][name] = {"ok": True, "count": len(raws)}
    except SourceError as exc:
        stats["sources"][name] = {"ok": False, "count": 0}
        errors[name] = str(exc)
    except Exception as exc:  # en trasig källa får inte stoppa övriga
        stats["sources"][name] = {"ok": False, "count": 0}
        errors[name] = f"{type(exc).__name__}: {exc}"


def _email_in_ad(email: str, job: Job) -> bool:
    """AI-adressen godtas bara om den är en hel adress som står i annonstexten."""
    found = {m.group().strip(".").casefold() for m in _ADDRESS_RE.finditer(job.description)}
    return email.casefold() in found


def _score_new_jobs(
    engine: Engine,
    profile: Profile,
    prefs: Preferences,
    ai: AIClient,
    stats: dict[str, Any],
    errors: dict[str, str],
    spent: list[float],
) -> None:
    """Poängsätter alla jobb med status new, även kvarlämnade från tidigare körningar."""
    with session_scope(engine) as session:
        job_ids = list(
            session.exec(select(Job.id).where(Job.status == JobStatus.new).order_by(Job.id))
        )

    for job_id in job_ids:
        try:
            with session_scope(engine) as session:
                job = session.get(Job, job_id)
                assert job is not None
                result = score_job(job, profile, prefs, ai)
                spent[0] += result.cost_usd
                scored = result.value
                job.score = scored.score
                job.score_reasons = scored.reasons
                email = (scored.apply_email or "").strip()
                if job.apply_method == ApplyMethod.unknown and _email_in_ad(email, job):
                    job.apply_method = ApplyMethod.email
                    job.apply_email = email
                transition(job, JobStatus.scored)
            stats["scored"] += 1
        except BudgetExceeded as exc:
            errors["budget"] = str(exc)
            break
        except AIError as exc:
            stats["scoring_failed"] += 1
            errors["scoring"] = str(exc)
