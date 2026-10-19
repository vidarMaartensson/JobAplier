from datetime import datetime, timedelta

import pytest
from fakes import FakeAI
from sqlmodel import select

from jobaplier.ai import AIError, BudgetExceeded
from jobaplier.config import Preferences
from jobaplier.db import session_scope
from jobaplier.models import ApplyMethod, Job, JobStatus, Run
from jobaplier.pipeline import run as run_module
from jobaplier.pipeline.run import RunInProgress, run_pipeline
from jobaplier.pipeline.scoring import ScoreResult
from jobaplier.profile.cv import Profile
from jobaplier.sources.base import ApplyInfo, RawJob, SourceError

NOW = datetime(2026, 10, 19, 12, 0, 0)
PROFILE = Profile(
    summary="Utvecklare",
    skills=["Python"],
    years_experience=5,
    roles=["Utvecklare"],
    languages=["sv"],
    education=[],
)


def score(n: int = 70, **kwargs) -> ScoreResult:
    return ScoreResult(
        score=n, reasons="Bra match.", apply_method_hint="unknown", **kwargs
    )


def raw(source: str, ext: str, title: str, company: str = "Exempel AB", **kwargs) -> RawJob:
    return RawJob(
        source=source,
        external_id=ext,
        title=title,
        company=company,
        location="Stockholm",
        remote=None,
        description=kwargs.pop("description", "Beskrivning"),
        url=f"https://example.se/{ext}",
        published_at=None,
        apply=ApplyInfo(method=ApplyMethod.unknown),
        **kwargs,
    )


class FakeSource:
    def __init__(self, name: str, jobs: list[RawJob] | None = None, error: Exception | None = None):
        self.name = name
        self.jobs = jobs or []
        self.error = error
        self.since: list[datetime] = []

    def fetch(self, since: datetime) -> list[RawJob]:
        self.since.append(since)
        if self.error is not None:
            raise self.error
        return list(self.jobs)


@pytest.fixture
def cv(make_pdf):
    return make_pdf("Anna Andersson\nPython-utvecklare")


def make_ai(scores: list) -> FakeAI:
    return FakeAI({Profile: [PROFILE], ScoreResult: list(scores)})


def go(engine, sources, ai, cv, prefs=None, **kwargs):
    return run_pipeline(
        engine,
        sources,
        prefs or Preferences(),
        ai,
        cv,
        trigger=kwargs.pop("trigger", "manual"),
        now=kwargs.pop("now", lambda: NOW),
    )


def statuses(engine) -> dict[str, JobStatus]:
    with session_scope(engine) as session:
        return {j.title: j.status for j in session.exec(select(Job).order_by(Job.id))}


def test_happy_path(engine, cv):
    a = FakeSource("a", [raw("a", "1", "Utvecklare"), raw("a", "2", "PHP-utvecklare")])
    b = FakeSource("b", [raw("b", "3", "Backend", company="Annat AB")])
    ai = make_ai([score(80), score(60)])

    run = go(engine, [a, b], ai, cv, Preferences(exclude_keywords=["php"]), trigger="schedule")

    assert statuses(engine) == {
        "Utvecklare": JobStatus.scored,
        "PHP-utvecklare": JobStatus.filtered,
        "Backend": JobStatus.scored,
    }
    assert run.stats_json == {
        "sources": {"a": {"ok": True, "count": 2}, "b": {"ok": True, "count": 1}},
        "new": 3,
        "filtered": 1,
        "scored": 2,
        "scoring_failed": 0,
    }
    assert run.errors_json == {}
    assert run.trigger == "schedule"
    assert run.started_at == NOW and run.finished_at == NOW
    assert run.ai_cost_usd == pytest.approx(0.002)  # bara poängsättning, inte profilbygget
    with session_scope(engine) as session:
        job = session.exec(select(Job).where(Job.title == "Utvecklare")).one()
        assert (job.score, job.score_reasons) == (80, "Bra match.")
        php = session.exec(select(Job).where(Job.title == "PHP-utvecklare")).one()
        assert php.filter_reason and php.score is None


def test_failing_source_does_not_stop_others(engine, cv):
    bad = FakeSource("bad", error=SourceError("x"))
    good = FakeSource("good", [raw("good", "1", "Utvecklare")])

    run = go(engine, [bad, good], make_ai([score()]), cv)

    assert statuses(engine) == {"Utvecklare": JobStatus.scored}
    assert run.errors_json == {"bad": "x"}
    assert run.stats_json["sources"]["bad"]["ok"] is False
    assert run.stats_json["sources"]["good"] == {"ok": True, "count": 1}


def test_unexpected_source_exception_is_recorded(engine, cv):
    bad = FakeSource("bad", error=ValueError("boom"))
    good = FakeSource("good", [raw("good", "1", "Utvecklare")])

    run = go(engine, [bad, good], make_ai([score()]), cv)

    assert run.errors_json == {"bad": "ValueError: boom"}
    assert run.stats_json["sources"]["bad"]["ok"] is False
    assert statuses(engine) == {"Utvecklare": JobStatus.scored}


def test_ai_error_for_one_job_keeps_it_new(engine, cv):
    src = FakeSource(
        "a",
        [
            raw("a", "1", "Ett"),
            raw("a", "2", "Två", company="B AB"),
            raw("a", "3", "Tre", company="C AB"),
        ],
    )
    ai = make_ai([score(), AIError("refusal"), score()])

    run = go(engine, [src], ai, cv)

    assert statuses(engine) == {
        "Ett": JobStatus.scored,
        "Två": JobStatus.new,
        "Tre": JobStatus.scored,
    }
    assert run.stats_json["scored"] == 2
    assert run.stats_json["scoring_failed"] == 1
    assert run.errors_json == {"scoring": "refusal"}

    # Nästa körning poängsätter det kvarlämnade jobbet (källan ger inget nytt).
    ai2 = make_ai([score(55)])
    run2 = go(engine, [FakeSource("a")], ai2, cv)

    assert set(statuses(engine).values()) == {JobStatus.scored}
    assert run2.stats_json["scored"] == 1
    assert run2.stats_json["new"] == 0
    assert [c["purpose"] for c in ai2.calls] == ["scoring"]  # profilen är cachad
    assert run2.errors_json == {}


def test_budget_stops_scoring(engine, cv):
    src = FakeSource("a", [raw("a", "1", "Ett"), raw("a", "2", "Två", company="B AB")])
    msg = "AI-budgeten för månaden är slut"
    ai = make_ai([BudgetExceeded(msg), score()])

    run = go(engine, [src], ai, cv)

    assert set(statuses(engine).values()) == {JobStatus.new}
    assert run.errors_json == {"budget": msg}
    assert run.stats_json["scored"] == 0
    assert len([c for c in ai.calls if c["purpose"] == "scoring"]) == 1  # avbröts direkt
    assert run.finished_at is not None


def test_since_per_source(engine, cv):
    previous = datetime(2026, 10, 18, 8, 0, 0)
    with session_scope(engine) as session:
        session.add(
            Run(
                started_at=previous - timedelta(days=1),
                stats_json={"sources": {"ok": {"ok": True, "count": 1}}},
            )
        )
        session.add(
            Run(
                started_at=previous,
                stats_json={
                    "sources": {
                        "ok": {"ok": True, "count": 1},
                        "broken": {"ok": False, "count": 0},
                    }
                },
            )
        )
    ok, broken, fresh = FakeSource("ok"), FakeSource("broken"), FakeSource("fresh")

    go(engine, [ok, broken, fresh], make_ai([]), cv)

    assert ok.since == [previous]
    assert broken.since == [NOW - timedelta(days=14)]
    assert fresh.since == [NOW - timedelta(days=14)]


def test_since_uses_earlier_successful_run_not_current(engine, cv):
    src = FakeSource("a")
    first_now = NOW - timedelta(hours=4)
    go(engine, [src], make_ai([]), cv, now=lambda: first_now)
    go(engine, [src], make_ai([]), cv)

    assert src.since == [first_now - timedelta(days=14), first_now]


def test_concurrent_run_raises(engine, cv):
    with run_module._run_lock:
        with pytest.raises(RunInProgress):
            go(engine, [FakeSource("a")], make_ai([]), cv)
    with session_scope(engine) as session:
        assert session.exec(select(Run)).all() == []


def test_lock_released_after_unexpected_failure(engine, cv):
    src = FakeSource("a", [raw("a", "1", "Utvecklare")])

    with pytest.raises(RuntimeError):
        go(engine, [src], make_ai([RuntimeError("bug")]), cv)

    assert run_module._run_lock.acquire(blocking=False)
    run_module._run_lock.release()
    with session_scope(engine) as session:
        (run,) = session.exec(select(Run)).all()
        assert run.finished_at is not None
        assert run.stats_json["sources"]["a"]["ok"] is True


def test_ai_found_email_upgrades_method(engine, cv):
    src = FakeSource("a", [raw("a", "1", "Utvecklare", description="Mejla hr@x.se")])

    go(engine, [src], make_ai([score(apply_email="hr@x.se")]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email
        assert job.apply_email == "hr@x.se"


@pytest.mark.parametrize("ai_email", ["N/A", "none", "other@x.se", "hr@x"])
def test_unverified_ai_email_leaves_method_unknown(engine, cv, ai_email):
    src = FakeSource("a", [raw("a", "1", "Utvecklare", description="Mejla hr@x.se")])

    go(engine, [src], make_ai([score(77, apply_email=ai_email)]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.status == JobStatus.scored and job.score == 77
        assert job.apply_method == ApplyMethod.unknown
        assert job.apply_email is None


def test_ai_email_matches_description_case_insensitively(engine, cv):
    src = FakeSource("a", [raw("a", "1", "Utvecklare", description="Mejla HR@X.se")])

    go(engine, [src], make_ai([score(apply_email="hr@x.se")]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email


def test_truncated_ai_email_is_rejected(engine, cv):
    desc = "Skicka ansökan till rekrytering@foretag.se"
    src = FakeSource("a", [raw("a", "1", "Utvecklare", description=desc)])

    go(engine, [src], make_ai([score(apply_email="ekrytering@foretag.se")]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.status == JobStatus.scored
        assert job.apply_method == ApplyMethod.unknown
        assert job.apply_email is None


@pytest.mark.parametrize(
    "description",
    [
        "Skicka din ansökan till rekrytering@foretag.se.",
        "Kontakt (rekrytering@foretag.se), tack!",
        "Mejla: <rekrytering@foretag.se>; vi hör av oss",
        "Frågor? Rekrytering@Foretag.se\nHälsningar",
    ],
)
def test_ai_email_accepted_in_running_text(engine, cv, description):
    src = FakeSource("a", [raw("a", "1", "Utvecklare", description=description)])

    go(engine, [src], make_ai([score(apply_email="rekrytering@foretag.se")]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.email
        assert job.apply_email == "rekrytering@foretag.se"


def test_cost_survives_unexpected_scoring_exception(engine, cv):
    src = FakeSource("a", [raw("a", "1", "Ett"), raw("a", "2", "Två", company="B AB")])

    with pytest.raises(RuntimeError):
        go(engine, [src], make_ai([score(), RuntimeError("bug")]), cv)

    with session_scope(engine) as session:
        (run,) = session.exec(select(Run)).all()
        assert run.ai_cost_usd == pytest.approx(0.001)


def test_ai_email_does_not_override_known_method(engine, cv):
    job_raw = raw("a", "1", "Utvecklare")
    job_raw.apply = ApplyInfo(method=ApplyMethod.form, form_url="https://x.se/form")

    go(engine, [FakeSource("a", [job_raw])], make_ai([score(apply_email="hr@x.se")]), cv)

    with session_scope(engine) as session:
        job = session.exec(select(Job)).one()
        assert job.apply_method == ApplyMethod.form
        assert job.apply_email is None


def test_missing_cv_recorded(engine, tmp_path):
    src = FakeSource("a", [raw("a", "1", "Utvecklare")])
    ai = make_ai([])

    run = go(engine, [src], ai, tmp_path / "saknas.pdf")

    assert "profile" in run.errors_json
    assert ai.calls == []
    assert src.since == []
    assert statuses(engine) == {}
    assert run.finished_at is not None
    assert run.stats_json["scored"] == 0
