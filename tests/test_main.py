import threading
from datetime import datetime

import pytest
from fakes import FakeGmail
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.config import CareerSite, Config, ProfileInfo, Toggle, save_config
from jobaplier.db import init_db, make_engine, session_scope
from jobaplier.gmail import GmailAuthError
from jobaplier.main import (
    EMAIL_INTERRUPTED,
    LINKEDIN_NO_GMAIL,
    ConfigHolder,
    ConfiguredAI,
    LazyGmail,
    Runner,
    build_runtime,
    build_sources,
    build_state,
    create_production_app,
    sweep_interrupted_emails,
)
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Job,
    JobStatus,
    Run,
)
from jobaplier.pipeline.run import RunInProgress
from jobaplier.sources.base import SourceError
from jobaplier.sources.careers.greenhouse import GreenhouseSource
from jobaplier.sources.careers.teamtailor import TeamtailorSource
from jobaplier.sources.http import make_http_client
from jobaplier.sources.linkedin_alerts import LinkedInAlertsSource
from jobaplier.sources.platsbanken import PlatsbankenSource
from jobaplier.web.setup import CONFIG_MISSING, CV_MISSING, GMAIL_MISSING

LOCAL = "http://127.0.0.1:8000"


@pytest.fixture
def http():
    client = make_http_client()
    yield client
    client.close()


@pytest.fixture
def runtimes():
    """Stänger varje runtime som testet bygger (motor, http-klient, schemaläggare)."""
    built = []

    def _build(paths, **kwargs):
        runtime = build_runtime(paths, **kwargs)
        built.append(runtime)
        return runtime

    yield _build
    for runtime in built:
        runtime.close()
        runtime.state.engine.dispose()


def _config(**sources) -> Config:
    config = Config(profile=ProfileInfo(name="Test Testsson", email="test@example.se"))
    for name, value in sources.items():
        setattr(config.sources, name, value)
    return config


# --- källor ---------------------------------------------------------------------------


def test_build_sources_respects_toggles(paths, http):
    config = _config(
        platsbanken=Toggle(enabled=False),
        linkedin_alerts=Toggle(enabled=False),
        careers=[
            CareerSite(type="greenhouse", company="Acme", id="acme"),
            CareerSite(type="teamtailor", company="Exempel AB", id="exempel"),
        ],
    )

    sources = build_sources(config, paths, http)

    assert not any(isinstance(s, PlatsbankenSource) for s in sources)
    assert [type(s) for s in sources] == [GreenhouseSource, TeamtailorSource]


def test_build_sources_platsbanken_enabled(paths, http):
    config = _config(linkedin_alerts=Toggle(enabled=False))

    sources = build_sources(config, paths, http)

    assert [type(s) for s in sources] == [PlatsbankenSource]
    assert sources[0].prefs is config.preferences


def test_build_sources_linkedin_without_gmail_token_reports_error(paths, http, caplog):
    config = _config(platsbanken=Toggle(enabled=False))

    sources = build_sources(config, paths, http)

    assert [s.name for s in sources] == ["linkedin_alerts"]
    assert not isinstance(sources[0], LinkedInAlertsSource)
    with pytest.raises(SourceError) as exc_info:
        sources[0].fetch(None)
    assert str(exc_info.value) == LINKEDIN_NO_GMAIL
    assert "LinkedIn" in caplog.text


def test_build_sources_linkedin_with_gmail_token(paths, http):
    paths.gmail_token.write_text("{}")
    gmail = FakeGmail()
    config = _config(platsbanken=Toggle(enabled=False))

    sources = build_sources(config, paths, http, gmail=gmail)

    assert [type(s) for s in sources] == [LinkedInAlertsSource]
    assert sources[0].gmail is gmail


# --- start utan installation -------------------------------------------------------


def test_build_state_without_setup_shows_setup_page(tmp_path):
    from jobaplier.config import Paths

    paths = Paths.from_data_dir(tmp_path / "ny-data")  # katalogen finns inte ännu
    state = build_state(paths)
    try:
        missing = state.setup_missing()
        assert CV_MISSING in missing
        assert CONFIG_MISSING in missing
        assert GMAIL_MISSING in missing
        with pytest.raises(FileNotFoundError):
            state.config()

        from jobaplier.web.app import create_app

        client = TestClient(create_app(state), base_url=LOCAL)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
        setup = client.get("/setup")
        assert setup.status_code == 200
        assert "CV saknas" in setup.text
    finally:
        state.engine.dispose()


def test_invalid_config_counts_as_missing(paths, runtimes):
    paths.config.write_text("profile: [inte, en, profil]\n", encoding="utf-8")

    state = runtimes(paths).state

    assert CONFIG_MISSING in state.setup_missing()
    with pytest.raises(ValueError):
        state.config()


# --- inställningar ---------------------------------------------------------------------


def test_on_config_saved_updates_config_and_schedule(paths, runtimes):
    first = _config()
    first.schedule = ["08:00"]
    save_config(first, paths.config)
    runtime = runtimes(paths)
    assert runtime.state.config().schedule == ["08:00"]
    assert [j.id for j in runtime.scheduler.get_jobs()] == ["run-0800"]

    second = _config()
    second.schedule = ["09:30", "18:00"]
    save_config(second, paths.config)
    runtime.state.on_config_saved(second)

    assert runtime.state.config() is second
    assert sorted(j.id for j in runtime.scheduler.get_jobs()) == ["run-0930", "run-1800"]


def test_schedule_without_config_uses_defaults(paths, runtimes):
    runtime = runtimes(paths)
    assert sorted(j.id for j in runtime.scheduler.get_jobs()) == [
        "run-0800", "run-1200", "run-1700",
    ]  # fmt: skip


def test_config_holder_picks_up_file_created_after_start(paths):
    holder = ConfigHolder(paths.config)
    assert holder.current() is None

    save_config(_config(), paths.config)

    assert holder.current() is not None
    assert holder.get().profile.email == "test@example.se"


# --- startsvep ---------------------------------------------------------------------------


def _job_with_application(engine, n: int, method: ApplyMethod, channel: Channel) -> int:
    with session_scope(engine) as session:
        job = Job(
            dedupe_key=f"k{n}",
            title=f"Utvecklare {n}",
            company="Exempel AB",
            apply_method=method,
            status=JobStatus.submitting,
        )
        session.add(job)
        session.flush()
        session.add(
            Application(job_id=job.id, channel=channel, status=ApplicationStatus.submitting)
        )
        return job.id


def _job_and_app(engine, job_id: int) -> tuple[Job, Application]:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        app = session.exec(select(Application).where(Application.job_id == job_id)).one()
        session.expunge_all()
        return job, app


def test_startup_sweep_fails_interrupted_emails_only(paths, runtimes):
    engine = make_engine(paths.db)
    init_db(engine)
    email_job = _job_with_application(engine, 1, ApplyMethod.email, Channel.email)
    form_job = _job_with_application(engine, 2, ApplyMethod.form, Channel.form)
    engine.dispose()

    runtime = runtimes(paths)

    job, app = _job_and_app(runtime.state.engine, email_job)
    assert job.status == JobStatus.failed
    assert app.status == ApplicationStatus.failed
    assert app.error == EMAIL_INTERRUPTED
    assert EMAIL_INTERRUPTED == (
        "Avbröts under utskick – kontrollera Skickat i Gmail innan du försöker igen"
    )
    job, app = _job_and_app(runtime.state.engine, form_job)
    assert job.status == JobStatus.submitting
    assert app.status == ApplicationStatus.submitting
    assert app.error is None


def test_sweep_is_skipped_when_disabled(paths, runtimes):
    engine = make_engine(paths.db)
    init_db(engine)
    email_job = _job_with_application(engine, 1, ApplyMethod.email, Channel.email)
    engine.dispose()

    runtime = runtimes(paths, startup_sweep=False)

    job, _app = _job_and_app(runtime.state.engine, email_job)
    assert job.status == JobStatus.submitting


def test_sweep_returns_count(engine):
    _job_with_application(engine, 1, ApplyMethod.email, Channel.email)
    _job_with_application(engine, 2, ApplyMethod.email, Channel.email)
    assert sweep_interrupted_emails(engine) == 2
    assert sweep_interrupted_emails(engine) == 0


def _interrupted_state(paths) -> tuple[int, int, int, int]:
    """Ett brev som skrevs och en körning som pågick när processen dog, plus orörda."""
    engine = make_engine(paths.db)
    init_db(engine)
    with session_scope(engine) as session:
        drafting = Job(dedupe_key="d", title="T", company="C", status=JobStatus.drafting)
        scored = Job(dedupe_key="s", title="T", company="C", status=JobStatus.scored)
        running = Run(started_at=datetime(2026, 10, 19, 6, 0), errors_json={"x": "fel"})
        done = Run(
            started_at=datetime(2026, 10, 19, 5, 0),
            finished_at=datetime(2026, 10, 19, 5, 1),
            errors_json={"platsbanken": "nere"},
        )
        session.add_all([drafting, scored, running, done])
        session.flush()
        ids = (drafting.id, scored.id, running.id, done.id)
    engine.dispose()
    return ids


def test_startup_sweep_resets_drafting_jobs_and_closes_unfinished_runs(paths, runtimes):
    drafting, scored, running, done = _interrupted_state(paths)

    runtime = runtimes(paths)

    with Session(runtime.state.engine) as session:
        assert session.get(Job, drafting).status == JobStatus.scored
        assert session.get(Job, scored).status == JobStatus.scored
        run = session.get(Run, running)
        assert run.finished_at is not None
        assert run.errors_json == {
            "x": "fel",
            "avbruten": "Körningen avbröts (tjänsten stängdes)",
        }
        finished = session.get(Run, done)
        assert finished.finished_at == datetime(2026, 10, 19, 5, 1)
        assert finished.errors_json == {"platsbanken": "nere"}


def test_run_once_does_not_sweep_drafts_or_runs(paths, runtimes):
    drafting, _scored, running, _done = _interrupted_state(paths)

    runtime = runtimes(paths, startup_sweep=False)

    with Session(runtime.state.engine) as session:
        assert session.get(Job, drafting).status == JobStatus.drafting
        assert session.get(Run, running).finished_at is None


# --- körningar ---------------------------------------------------------------------------


def _runs(engine) -> list[Run]:
    with Session(engine) as session:
        runs = list(session.exec(select(Run).order_by(Run.id)))
        session.expunge_all()
        return runs


def test_run_now_without_config_records_failed_run(paths, runtimes):
    runtime = runtimes(paths)

    runtime.state.run_now()
    runtime.runner.join(timeout=10)

    [run] = _runs(runtime.state.engine)
    assert run.trigger == "manual"
    assert run.finished_at is not None
    assert run.errors_json == {"config": CONFIG_MISSING}


def test_run_now_without_cv_records_profile_error(paths, runtimes):
    save_config(
        _config(platsbanken=Toggle(enabled=False), linkedin_alerts=Toggle(enabled=False)),
        paths.config,
    )
    runtime = runtimes(paths)

    runtime.state.run_now()
    runtime.runner.join(timeout=10)

    [run] = _runs(runtime.state.engine)
    assert run.finished_at is not None
    assert "CV-filen saknas" in run.errors_json["profile"]


def test_runner_rejects_concurrent_runs():
    started = threading.Event()
    release = threading.Event()
    triggers: list[str] = []

    def execute(trigger: str) -> None:
        triggers.append(trigger)
        started.set()
        release.wait(timeout=10)

    runner = Runner(execute)
    runner.start()
    assert started.wait(timeout=10)

    with pytest.raises(RunInProgress):
        runner.start()
    runner.run_scheduled()  # hoppas över medan en körning pågår
    with pytest.raises(RunInProgress):
        runner.run_sync("manual")

    release.set()
    runner.join(timeout=10)
    runner.run_scheduled()
    assert triggers == ["manual", "schedule"]


def test_runner_survives_crashing_run(caplog):
    def execute(trigger: str) -> None:
        raise RuntimeError("trasig")

    runner = Runner(execute)
    runner.start()
    runner.join(timeout=10)
    runner.run_scheduled()

    assert "trasig" in caplog.text
    runner.start()  # låset är släppt trots kraschen
    runner.join(timeout=10)


# --- AI och Gmail skapas när de behövs ----------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.built: list[str] = []

    def __call__(self, config, engine, paths):
        self.built.append(config.ai.backend)
        return f"ai-{len(self.built)}"


def test_configured_ai_follows_config(paths, engine):
    holder = ConfigHolder(paths.config)
    factory = _Recorder()
    ai = ConfiguredAI(holder.current, engine, paths, factory=factory)

    with pytest.raises(AIError):
        ai.current()

    config = _config()
    holder.set(config)
    assert ai.current() == "ai-1"
    assert ai.current() == "ai-1"  # oförändrad AI-konfiguration återanvänds

    changed = _config()
    changed.ai.backend = "api"
    holder.set(changed)
    assert ai.current() == "ai-2"
    assert factory.built == ["claude_code", "api"]


def test_lazy_gmail_retries_after_auth_error():
    created: list[FakeGmail] = []
    failures = [GmailAuthError("token saknas")]

    def factory() -> FakeGmail:
        if failures:
            raise failures.pop()
        created.append(FakeGmail())
        return created[-1]

    gmail = LazyGmail(factory)
    with pytest.raises(GmailAuthError):
        gmail.search("q")
    gmail.search("q1")
    gmail.search("q2")
    assert len(created) == 1
    assert created[0].queries == ["q1", "q2"]

    created[0].search = lambda query: (_ for _ in ()).throw(GmailAuthError("utgången"))
    with pytest.raises(GmailAuthError):
        gmail.search("q3")
    gmail.search("q4")  # ny klient läses in från token-filen
    assert len(created) == 2


def test_lazy_gmail_rereads_token_after_refresh_error_in_execute():
    """Utgången token mitt i processen: nästa anrop läser token-filen igen, så att
    `jobaplier gmail-login` räcker utan omstart."""
    from google.auth.exceptions import RefreshError

    from jobaplier.gmail import GoogleGmail

    class _Expired:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **_kwargs):
            return self

        def execute(self):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    fresh = FakeGmail()
    clients = [GoogleGmail(_Expired()), fresh]
    gmail = LazyGmail(lambda: clients.pop(0))

    with pytest.raises(GmailAuthError, match="jobaplier gmail-login"):
        gmail.search("q1")
    gmail.search("q2")
    assert clients == []
    assert fresh.queries == ["q2"]


# --- produktionsappen ----------------------------------------------------------------


def test_production_app_lifespan_starts_and_stops_scheduler(paths, monkeypatch):
    app = create_production_app(paths)
    runtime = app.state.runtime
    closed: list[bool] = []
    monkeypatch.setattr(runtime.form_applier, "close", lambda: closed.append(True))
    try:
        with TestClient(app, base_url=LOCAL) as client:
            assert runtime.scheduler.running
            assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
            assert client.get("/", headers={"host": "evil.example"}).status_code == 403
        assert not runtime.scheduler.running
        assert closed == [True]
    finally:
        runtime.state.engine.dispose()
