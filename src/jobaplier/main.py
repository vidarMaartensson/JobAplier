"""Produktionskoppling: riktiga komponenter, schemaläggare och FastAPI-appen.

Allt som kräver installationssteg (inställningar, AI-backend) skapas
först när det behövs, så att dashboarden alltid startar och kan visa /setup.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlmodel import col, select

from jobaplier.ai import AIClient, AIError, AIResult, make_ai
from jobaplier.apply.form import FormApplier, default_adapters
from jobaplier.apply.package import build_cv_pdf, cv_filename
from jobaplier.apply.service import ApplyService
from jobaplier.config import Config, Paths, anthropic_api_key, get_paths, load_config
from jobaplier.db import init_db, make_engine, session_scope
from jobaplier.models import (
    ApplyMethod,
    Draft,
    Job,
    JobStatus,
    Run,
    utcnow,
)
from jobaplier.pipeline.run import RunInProgress, run_pipeline
from jobaplier.profile.cv import CVMissing, CVUnreadable
from jobaplier.profile.cv_document import (
    CVDocument,
    cached_cv_document,
    cv_document_warnings,
)
from jobaplier.scheduler import build_scheduler, reschedule
from jobaplier.sources.base import Source
from jobaplier.sources.careers import build_career_sources
from jobaplier.sources.http import make_http_client
from jobaplier.sources.linkedin_links import LinkedInLinksSource
from jobaplier.sources.platsbanken import PlatsbankenSource
from jobaplier.status import transition
from jobaplier.web.app import create_app
from jobaplier.web.setup import CONFIG_MISSING, compute_setup_missing
from jobaplier.web.state import AppState

log = logging.getLogger(__name__)

RUN_INTERRUPTED = "Körningen avbröts (tjänsten stängdes)"
CONFIG_INVALID = (
    "Inställningarna i data/config.yaml går inte att läsa – rätta dem under Inställningar"
)
DEFAULT_SCHEDULE: list[str] = Config.model_fields["schedule"].get_default(
    call_default_factory=True
)
# Fel som `load_config` ger för en saknad, oläsbar eller ogiltig config.yaml.
CONFIG_ERRORS = (OSError, ValueError, yaml.YAMLError)


# --- inställningar ---------------------------------------------------------------------


class ConfigHolder:
    """Senast sparade inställningar. Läses från fil tills en giltig fil finns, och
    ersätts sedan bara via `set` (när inställningarna sparas i dashboarden)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config: Config | None = None
        self.current()

    def get(self) -> Config:
        """Kastar `OSError`, `ValueError` eller `yaml.YAMLError` om filen saknas eller
        är ogiltig."""
        with self._lock:
            if self._config is None:
                self._config = load_config(self._path)
            return self._config

    def current(self) -> Config | None:
        try:
            return self.get()
        except CONFIG_ERRORS:
            return None

    def set(self, config: Config) -> None:
        with self._lock:
            self._config = config


# --- AI skapas när den behövs ------------------------------------------------------------


class ConfiguredAI(AIClient):
    """AI-klient enligt aktuella inställningar. Byggs om när AI-inställningarna ändras."""

    def __init__(
        self,
        config: Callable[[], Config | None],
        engine: Engine,
        paths: Paths,
        factory: Callable[[Config, Engine, Paths], AIClient] = make_ai,
    ) -> None:
        self._config = config
        self._engine = engine
        self._paths = paths
        self._factory = factory
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._client: AIClient | None = None

    def current(self) -> AIClient:
        """Kastar `AIError` om inställningarna saknas eller backenden inte kan skapas."""
        config = self._config()
        if config is None:
            raise AIError(CONFIG_MISSING)
        key = (config.ai.model_dump(), config.ai_monthly_budget_usd)
        with self._lock:
            if self._client is None or key != self._key:
                self._client = self._factory(config, self._engine, self._paths)
                self._key = key
            return self._client

    def complete(self, **kwargs: Any) -> AIResult:
        return self.current().complete(**kwargs)


# --- källor ------------------------------------------------------------------------------


def build_sources(config: Config, http: httpx.Client) -> list[Source]:
    """De aktiverade källorna."""
    sources: list[Source] = []
    if config.sources.platsbanken.enabled:
        sources.append(PlatsbankenSource(config.preferences, http))
    sources.extend(build_career_sources(config.sources.careers, http))
    return sources


# --- körningar ---------------------------------------------------------------------------


class Runner:
    """Högst en körning åt gången i processen: "Sök nu", schemat och `run-once`."""

    def __init__(self, execute: Callable[[str, list[Source] | None], Run | None]) -> None:
        self._execute = execute
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, sources: list[Source] | None = None) -> None:
        """Startar en manuell körning i en bakgrundstråd, med de konfigurerade källorna
        eller (om `sources` anges) bara de angivna. Kastar `RunInProgress` direkt om en
        körning redan pågår."""
        self._acquire()
        try:
            thread = threading.Thread(
                target=self._guarded,
                args=("manual", sources),
                name="jobaplier-run",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except BaseException:
            self._lock.release()
            raise

    def run_scheduled(self) -> None:
        """Schemalagd körning; hoppas över om en körning redan pågår."""
        if not self._lock.acquire(blocking=False):
            log.info("Schemalagd körning hoppas över: en körning pågår redan")
            return
        self._guarded("schedule")

    def run_sync(self, trigger: str) -> Run | None:
        self._acquire()
        try:
            return self._execute(trigger, None)
        finally:
            self._lock.release()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _acquire(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise RunInProgress("En körning pågår redan")

    def _guarded(self, trigger: str, sources: list[Source] | None = None) -> None:
        """Kör med låset redan taget och släpper det; fel loggas i stället för att kastas."""
        try:
            run = self._execute(trigger, sources)
            if run is not None:
                log.info("Körning %s klar: %s %s", run.id, run.stats_json, run.errors_json)
        except Exception:
            log.exception("Körningen (%s) kraschade", trigger)
        finally:
            self._lock.release()


def record_failed_run(engine: Engine, trigger: str, errors: dict[str, str]) -> Run:
    with session_scope(engine) as session:
        now = utcnow()
        run = Run(started_at=now, finished_at=now, trigger=trigger, errors_json=errors)
        session.add(run)
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run


def execute_run(
    engine: Engine,
    paths: Paths,
    holder: ConfigHolder,
    ai: ConfiguredAI,
    http: httpx.Client,
    trigger: str,
    sources: list[Source] | None = None,
) -> Run:
    """En körning med aktuella inställningar, med de konfigurerade källorna eller (om
    `sources` anges) bara de angivna. Saknas något som krävs innan källorna
    kan hämtas sparas en misslyckad körning som syns i dashboarden."""
    try:
        config = holder.get()
    except FileNotFoundError:
        return record_failed_run(engine, trigger, {"config": CONFIG_MISSING})
    except CONFIG_ERRORS:
        return record_failed_run(engine, trigger, {"config": CONFIG_INVALID})
    try:
        ai.current()
    except AIError as exc:
        return record_failed_run(engine, trigger, {"ai": str(exc)})
    if sources is None:
        sources = build_sources(config, http)
    return run_pipeline(
        engine, sources, config.preferences, ai, paths.cv, trigger=trigger  # type: ignore[arg-type]
    )


# --- startsvep -----------------------------------------------------------------------------


def sweep_interrupted_drafts(engine: Engine) -> int:
    """Brev som skrevs när processen dog: jobbet blir poängsatt igen och kan sökas på nytt."""
    with session_scope(engine) as session:
        jobs = session.exec(select(Job).where(Job.status == JobStatus.drafting)).all()
        for job in jobs:
            transition(job, JobStatus.scored)
        return len(jobs)


def sweep_unfinished_runs(engine: Engine) -> int:
    """Körningar som aldrig avslutades (processen dog) visas inte längre som pågående."""
    with session_scope(engine) as session:
        runs = session.exec(select(Run).where(col(Run.finished_at).is_(None))).all()
        now = utcnow()
        for run in runs:
            run.finished_at = now
            run.errors_json["avbruten"] = RUN_INTERRUPTED
        return len(runs)


def run_startup_sweep(engine: Engine) -> None:
    """Städar upp efter en process som dog mitt i arbetet. Bara vid `serve`."""
    if count := sweep_interrupted_drafts(engine):
        log.warning("%d brev avbröts medan de skrevs; jobben är poängsatta igen", count)
    if count := sweep_unfinished_runs(engine):
        log.warning("%d körningar avbröts och markerades som avslutade", count)


# --- koppling --------------------------------------------------------------------------------


def form_cv_pdf(
    engine: Engine, paths: Paths, config: Config, ai: AIClient, job: Job, draft: Draft
) -> Path:
    """CV:t som laddas upp i formuläret, med samma filnamn som nedladdningen
    (`data/tmp/<jobb-id>/CV_<Namn>_<Företag>.pdf`). Utan skräddarsytt CV kopieras
    originalet dit, så att arbetsgivaren alltid får ett begripligt filnamn."""
    assert draft.id is not None
    filename = cv_filename(config.profile.name or "CV", job.company)
    out = paths.data_dir / "tmp" / str(job.id) / filename
    path, _tailored = build_cv_pdf(engine, paths, config, ai, draft.id, out)
    if path != out:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
    return out


@dataclass
class Runtime:
    state: AppState
    scheduler: BackgroundScheduler
    runner: Runner
    form_applier: FormApplier
    http: httpx.Client

    def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.form_applier.close()
        self.http.close()


def build_runtime(paths: Paths, *, startup_sweep: bool = True) -> Runtime:
    """Bygger alla komponenter. Fungerar även när installationen är ofullständig.

    `startup_sweep=False` används av `run-once`: skulle tjänsten ändå vara igång (vilket
    inte stöds, se README) får dess pågående arbete inte städas bort."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    engine = make_engine(paths.db)
    init_db(engine)
    if startup_sweep:
        run_startup_sweep(engine)

    holder = ConfigHolder(paths.config)
    ai = ConfiguredAI(holder.current, engine, paths)
    http = make_http_client()

    form_applier = FormApplier(
        holder.get,
        paths,
        default_adapters(),
        cv_pdf=lambda job, draft: form_cv_pdf(engine, paths, holder.get(), ai, job, draft),
    )
    # En enda instans per process: dess lås garanterar att ett jobb skickas högst en gång.
    service = ApplyService(
        engine,
        ai,
        paths,
        holder.get,
        {ApplyMethod.form: form_applier},
    )

    runner = Runner(
        lambda trigger, sources: execute_run(engine, paths, holder, ai, http, trigger, sources)
    )
    config = holder.current()
    times = config.schedule if config is not None else DEFAULT_SCHEDULE
    scheduler = build_scheduler(runner.run_scheduled, times)

    def on_config_saved(new: Config) -> None:
        holder.set(new)
        reschedule(scheduler, runner.run_scheduled, new.schedule)

    def cv_pdf(draft_id: int, out: Path) -> tuple[Path, bool]:
        return build_cv_pdf(engine, paths, holder.get(), ai, draft_id, out)

    def cv_document() -> CVDocument | None:
        """Bara det cachade dokumentet: det byggs med första utkastet, aldrig av /cv."""
        try:
            with session_scope(engine) as session:
                return cached_cv_document(session, paths.cv)
        except (CVMissing, CVUnreadable, ValidationError):
            return None

    def cv_warnings() -> list[str]:
        try:
            with session_scope(engine) as session:
                return cv_document_warnings(session, paths.cv)
        except (CVMissing, CVUnreadable):
            return []

    state = AppState(
        engine=engine,
        paths=paths,
        config=holder.get,
        service=service,
        run_now=runner.start,
        import_links=lambda ids: runner.start(sources=[LinkedInLinksSource(ids, http)]),
        setup_missing=lambda: compute_setup_missing(paths, holder.current, anthropic_api_key),
        cv_pdf=cv_pdf,
        cv_document=cv_document,
        on_config_saved=on_config_saved,
        cv_warnings=cv_warnings,
    )
    return Runtime(
        state=state, scheduler=scheduler, runner=runner, form_applier=form_applier, http=http
    )


def build_state(paths: Paths) -> AppState:
    return build_runtime(paths).state


def create_production_app(paths: Paths | None = None) -> FastAPI:
    runtime = build_runtime(paths or get_paths())
    app = create_app(runtime.state)
    app.state.runtime = runtime

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.scheduler.start()
        jobs = runtime.scheduler.get_jobs()
        upcoming = sorted(f"{job.next_run_time:%Y-%m-%d %H:%M %Z}" for job in jobs)
        log.info("Schemaläggaren startad, kommande körningar: %s", ", ".join(upcoming))
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app.router.lifespan_context = lifespan
    return app
