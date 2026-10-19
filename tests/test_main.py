import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.apply.form import VarbiAdapter
from jobaplier.apply.tailor import TailoredCV, TailoredExperience
from jobaplier.config import CareerSite, Config, ProfileInfo, Toggle, save_config
from jobaplier.db import init_db, make_engine, session_scope
from jobaplier.main import (
    ConfigHolder,
    ConfiguredAI,
    Runner,
    build_runtime,
    build_sources,
    build_state,
    create_production_app,
)
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    CVDocumentRecord,
    CVDocumentWarnings,
    Channel,
    Draft,
    Job,
    JobStatus,
    Run,
    TailoredCVRecord,
)
from jobaplier.pipeline.run import RunInProgress
from jobaplier.profile.cv import cv_file_hash, extract_cv_text
from jobaplier.profile.cv_document import CVDocument, Experience
from jobaplier.sources.careers.greenhouse import GreenhouseSource
from jobaplier.sources.careers.teamtailor import TeamtailorSource
from jobaplier.sources.http import make_http_client
from jobaplier.sources.linkedin_links import LinkedInLinksSource
from jobaplier.sources.platsbanken import PlatsbankenSource
from jobaplier.web.setup import CONFIG_MISSING, CV_MISSING

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
        careers=[
            CareerSite(type="greenhouse", company="Acme", id="acme"),
            CareerSite(type="teamtailor", company="Exempel AB", id="exempel"),
        ],
    )

    sources = build_sources(config, http)

    assert not any(isinstance(s, PlatsbankenSource) for s in sources)
    assert [type(s) for s in sources] == [GreenhouseSource, TeamtailorSource]


def test_build_sources_platsbanken_enabled(paths, http):
    config = _config()

    sources = build_sources(config, http)

    assert [type(s) for s in sources] == [PlatsbankenSource]
    assert sources[0].prefs is config.preferences


# --- start utan installation -------------------------------------------------------


def test_build_state_without_setup_shows_setup_page(tmp_path):
    from jobaplier.config import Paths

    paths = Paths.from_data_dir(tmp_path / "ny-data")  # katalogen finns inte ännu
    state = build_state(paths)
    try:
        missing = state.setup_missing()
        assert CV_MISSING in missing
        assert CONFIG_MISSING in missing
        assert not any("Gmail" in m for m in missing)
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


def test_startup_sweep_leaves_submitting_jobs_alone(paths, runtimes):
    """Mejlansökningar kan inte längre skickas; gamla rader rörs inte, och formulär i
    "skickas" väntar på användaren."""
    engine = make_engine(paths.db)
    init_db(engine)
    email_job = _job_with_application(engine, 1, ApplyMethod.email, Channel.email)
    form_job = _job_with_application(engine, 2, ApplyMethod.form, Channel.form)
    engine.dispose()

    runtime = runtimes(paths)

    for job_id in (email_job, form_job):
        job, app = _job_and_app(runtime.state.engine, job_id)
        assert job.status == JobStatus.submitting
        assert app.status == ApplicationStatus.submitting
        assert app.error is None


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
        _config(platsbanken=Toggle(enabled=False)),
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

    def execute(trigger: str, sources=None) -> None:
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


def test_runner_start_passes_given_sources_to_execute():
    seen: list = []

    def execute(trigger: str, sources=None) -> None:
        seen.append((trigger, sources))

    runner = Runner(execute)
    only = [object()]
    runner.start(sources=only)  # type: ignore[arg-type]
    runner.join(timeout=10)
    runner.start()
    runner.join(timeout=10)

    assert seen == [("manual", only), ("manual", None)]


def test_runner_survives_crashing_run(caplog):
    def execute(trigger: str, sources=None) -> None:
        raise RuntimeError("trasig")

    runner = Runner(execute)
    runner.start()
    runner.join(timeout=10)
    runner.run_scheduled()

    assert "trasig" in caplog.text
    runner.start()  # låset är släppt trots kraschen
    runner.join(timeout=10)


# --- import av LinkedIn-länkar ---------------------------------------------------------


class _PipelineRecorder:
    """Ersätter `run_pipeline`: sparar källorna och kan hålla körningen igång."""

    def __init__(self, block: bool = False) -> None:
        self.sources: list[list] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(self, engine, sources, prefs, ai, cv_path, trigger):
        self.sources.append(list(sources))
        self.started.set()
        self.release.wait(timeout=10)
        return Run(started_at=datetime(2026, 10, 19, 8, 0), trigger=trigger)


@pytest.fixture
def pipeline(monkeypatch):
    """Riktig `execute_run` men utan AI-backend och utan att källorna hämtas."""
    monkeypatch.setattr(ConfiguredAI, "current", lambda self: object())
    recorder = _PipelineRecorder()
    monkeypatch.setattr("jobaplier.main.run_pipeline", recorder)
    return recorder


def test_import_links_runs_only_linkedin_source(paths, runtimes, pipeline):
    save_config(_config(), paths.config)  # Platsbanken är på
    runtime = runtimes(paths)

    runtime.state.import_links(["4012345678", "4012345679"])
    runtime.runner.join(timeout=10)
    runtime.state.run_now()
    runtime.runner.join(timeout=10)

    [[imported], scheduled] = pipeline.sources
    assert isinstance(imported, LinkedInLinksSource)
    assert imported._job_ids == ["4012345678", "4012345679"]
    assert [type(s) for s in scheduled] == [PlatsbankenSource]


def test_import_links_uses_runner_lock(paths, runtimes, pipeline):
    save_config(_config(), paths.config)
    runtime = runtimes(paths)
    pipeline.release.clear()

    runtime.state.run_now()
    assert pipeline.started.wait(timeout=10)
    try:
        with pytest.raises(RunInProgress):
            runtime.state.import_links(["4012345678"])
    finally:
        pipeline.release.set()
        runtime.runner.join(timeout=10)

    assert len(pipeline.sources) == 1  # importen startade ingen körning


# --- skräddarsytt CV ------------------------------------------------------------------

CV_DOC = CVDocument(
    name="Jane Doe",
    experience=[
        Experience(
            id="e1",
            title="Backendutvecklare",
            employer="Acme",
            period="2018 – 2024",
            bullets=["Byggde betalflöden"],
        )
    ],
    skills=["Python"],
)
TAILORED = TailoredCV(
    language="sv",
    headline="Python-specialist",
    summary="Anpassad profiltext.",
    experience=[TailoredExperience(id="e1", bullets=["Skräddarsydd punkt om API:er"])],
    skills=["Python"],
    education_order=[],
)


def _install_cv(paths, make_pdf, text: str = "Jane Doe. Backendutvecklare på Acme.") -> None:
    shutil.copyfile(make_pdf(text), paths.cv)


def _cache_cv_document(engine, paths, document_json: dict | None = None) -> None:
    with session_scope(engine) as session:
        session.add(
            CVDocumentRecord(
                cv_hash=cv_file_hash(paths.cv),
                document_json=document_json or CV_DOC.model_dump(mode="json"),
            )
        )


def _draft(engine, paths, *, tailored: bool) -> tuple[Job, Draft]:
    with session_scope(engine) as session:
        job = Job(dedupe_key=f"k{tailored}", title="Utvecklare", company="Exempel AB")
        session.add(job)
        session.flush()
        draft = Draft(job_id=job.id, letter_text="Hej!")
        session.add(draft)
        session.flush()
        if tailored:
            session.add(
                TailoredCVRecord(
                    draft_id=draft.id,
                    cv_hash=cv_file_hash(paths.cv),
                    tailored_json=TAILORED.model_dump(mode="json"),
                )
            )
        session.flush()
        session.refresh(job)
        session.refresh(draft)
        session.expunge_all()
        return job, draft


@pytest.fixture
def no_ai(monkeypatch):
    """Inget riktigt AI-anrop får göras; försök sparas och ger ett fel."""
    calls: list[dict] = []

    def complete(self, **kwargs):
        calls.append(kwargs)
        raise AIError("inget AI i testerna")

    monkeypatch.setattr(ConfiguredAI, "complete", complete)
    return calls


def test_form_applier_gets_cv_provider(paths, runtimes, make_pdf, no_ai):
    save_config(_config(), paths.config)
    _install_cv(paths, make_pdf)
    runtime = runtimes(paths)
    engine = runtime.state.engine
    _cache_cv_document(engine, paths)
    job, draft = _draft(engine, paths, tailored=True)
    plain_job, plain_draft = _draft(engine, paths, tailored=False)

    path = runtime.form_applier.cv_pdf(job, draft)

    # Samma filnamn som nedladdningen (ruling I3), i en katalog per jobb.
    assert path == paths.data_dir / "tmp" / str(job.id) / "CV_Test_Testsson_Exempel_AB.pdf"
    text = extract_cv_text(path)
    assert "Skräddarsydd punkt om API:er" in text
    assert "Backendutvecklare" in text and "Acme" in text
    # Utan skräddarsytt CV laddas originalet upp, under samma sorts namn.
    plain = runtime.form_applier.cv_pdf(plain_job, plain_draft)
    assert plain == paths.data_dir / "tmp" / str(plain_job.id) / "CV_Test_Testsson_Exempel_AB.pdf"
    assert plain.read_bytes() == paths.cv.read_bytes()
    assert no_ai == []


VARBI_FORM = Path(__file__).parent / "fixtures" / "forms" / "varbi.html"


class UploadNames(VarbiAdapter):
    """Varbi-adaptern, som noterar filnamnen som CV- och brevfälten fick."""

    def fill(self, page, applicant):
        report = super().fill(page, applicant)
        self.uploaded = [
            page.eval_on_selector(selector, "e => e.files[0] && e.files[0].name")
            for selector in ("#attachment_cv", "#attachment_letter")
        ]
        return report


def test_form_fill_uploads_files_named_like_the_downloads(paths, runtimes, make_pdf, no_ai):
    save_config(_config(), paths.config)
    _install_cv(paths, make_pdf)
    runtime = runtimes(paths)
    _cache_cv_document(runtime.state.engine, paths)
    job, draft = _draft(runtime.state.engine, paths, tailored=True)
    job.apply_form_url = VARBI_FORM.as_uri()
    spy = UploadNames()
    applier = runtime.form_applier
    applier.adapters, applier.headless, applier.allow_file_urls = [spy], True, True

    applier.submit(job, draft)

    assert spy.uploaded == ["CV_Test_Testsson_Exempel_AB.pdf", "Personligt_brev_Exempel_AB.pdf"]
    assert no_ai == []


def test_state_cv_pdf_wired(paths, runtimes, make_pdf, tmp_path, no_ai):
    save_config(_config(), paths.config)
    _install_cv(paths, make_pdf)
    runtime = runtimes(paths)
    _cache_cv_document(runtime.state.engine, paths)
    _job, draft = _draft(runtime.state.engine, paths, tailored=True)
    out = tmp_path / "ut" / "cv.pdf"

    assert runtime.state.cv_pdf(draft.id, out) == (out, True)
    assert "Python-specialist" in extract_cv_text(out)
    assert no_ai == []


def test_cv_document_none_without_cv(paths, runtimes, no_ai):
    runtime = runtimes(paths)

    assert runtime.state.cv_document() is None
    assert no_ai == []


def test_cv_warnings_wired(paths, runtimes, make_pdf, no_ai):
    runtime = runtimes(paths)
    assert runtime.state.cv_warnings() == []  # inget CV
    _install_cv(paths, make_pdf)
    engine = runtime.state.engine
    _cache_cv_document(engine, paths)
    with session_scope(engine) as session:
        record = session.exec(select(CVDocumentRecord)).one()
        session.add(CVDocumentWarnings(cv_document_id=record.id, warnings_json=["Varning"]))

    assert runtime.state.cv_warnings() == ["Varning"]
    assert no_ai == []


def test_cv_document_is_cache_only(paths, runtimes, make_pdf, no_ai):
    _install_cv(paths, make_pdf)
    runtime = runtimes(paths)
    engine = runtime.state.engine

    assert runtime.state.cv_document() is None  # inget byggs för /cv
    _cache_cv_document(engine, paths)
    assert runtime.state.cv_document() == CV_DOC

    _install_cv(paths, make_pdf, "Ett helt annat CV.")
    assert runtime.state.cv_document() is None  # cachen gäller det gamla CV:t
    _cache_cv_document(engine, paths, {"experience": "inte en lista"})
    assert runtime.state.cv_document() is None  # ogiltig rad
    assert no_ai == []


# --- AI skapas när den behövs -------------------------------------------------------


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
