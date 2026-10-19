"""Produktionskoppling: riktiga komponenter, schemaläggare och FastAPI-appen.

Allt som kräver installationssteg (inställningar, Gmail-token, AI-backend) skapas
först när det behövs, så att dashboarden alltid startar och kan visa /setup.
"""

from __future__ import annotations

import asyncio
import email.message
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlmodel import col, select

from jobaplier.ai import AIClient, AIError, AIResult, make_ai
from jobaplier.apply.email import EmailApplier
from jobaplier.apply.form import FormApplier, default_adapters
from jobaplier.apply.service import ApplyService, emails_sent_today
from jobaplier.config import Config, Paths, anthropic_api_key, get_paths, load_config
from jobaplier.db import init_db, make_engine, session_scope
from jobaplier.gmail import Gmail, GmailAuthError, GoogleGmail, Mail
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Job,
    JobStatus,
    Run,
    utcnow,
)
from jobaplier.pipeline.run import RunInProgress, run_pipeline
from jobaplier.scheduler import build_scheduler, reschedule
from jobaplier.sources.base import RawJob, Source, SourceError
from jobaplier.sources.careers import build_career_sources
from jobaplier.sources.http import make_http_client
from jobaplier.sources.linkedin_alerts import LinkedInAlertsSource
from jobaplier.sources.platsbanken import PlatsbankenSource
from jobaplier.status import transition
from jobaplier.web.app import create_app
from jobaplier.web.setup import CONFIG_MISSING, compute_setup_missing
from jobaplier.web.state import AppState

log = logging.getLogger(__name__)

T = TypeVar("T")

EMAIL_INTERRUPTED = (
    "Avbröts under utskick – kontrollera Skickat i Gmail innan du försöker igen"
)
RUN_INTERRUPTED = "Körningen avbröts (tjänsten stängdes)"
LINKEDIN_NO_GMAIL = (
    "LinkedIn-aviseringar hoppades över: Gmail-inloggning saknas – kör `jobaplier gmail-login`"
)
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


# --- AI och Gmail skapas när de behövs -------------------------------------------------


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


class LazyGmail(Gmail):
    """Gmail-klient som läser token-filen vid första anropet.

    Ett inloggningsfel kastar klienten, så att nästa anrop läser en ny token (efter
    `jobaplier gmail-login`) utan omstart. Anropen serialiseras eftersom Googles
    HTTP-klient (httplib2) inte är trådsäker."""

    def __init__(self, factory: Callable[[], Gmail]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._gmail: Gmail | None = None

    @classmethod
    def from_paths(cls, paths: Paths) -> LazyGmail:
        return cls(lambda: GoogleGmail.from_files(paths.gmail_credentials, paths.gmail_token))

    def search(self, query: str) -> list[Mail]:
        return self._call(lambda gmail: gmail.search(query))

    def send(self, msg: email.message.EmailMessage) -> str:
        return self._call(lambda gmail: gmail.send(msg))

    def _call(self, fn: Callable[[Gmail], T]) -> T:
        with self._lock:
            if self._gmail is None:
                self._gmail = self._factory()
            try:
                return fn(self._gmail)
            except GmailAuthError:
                self._gmail = None
                raise


# --- källor ------------------------------------------------------------------------------


class UnavailableSource:
    """Står i en källas ställe när den inte kan användas; felet syns i körningen."""

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message

    def fetch(self, since: datetime) -> list[RawJob]:
        raise SourceError(self.message)


def build_sources(
    config: Config, paths: Paths, http: httpx.Client, gmail: Gmail | None = None
) -> list[Source]:
    """De aktiverade källorna. Utan Gmail-token hoppas LinkedIn över med ett fel i körningen."""
    sources: list[Source] = []
    if config.sources.platsbanken.enabled:
        sources.append(PlatsbankenSource(config.preferences, http))
    if config.sources.linkedin_alerts.enabled:
        if paths.gmail_token.is_file():
            sources.append(LinkedInAlertsSource(gmail or LazyGmail.from_paths(paths), http))
        else:
            log.warning(
                "LinkedIn-aviseringar hoppas över: Gmail-token saknas (%s)", paths.gmail_token
            )
            sources.append(UnavailableSource(LinkedInAlertsSource.name, LINKEDIN_NO_GMAIL))
    sources.extend(build_career_sources(config.sources.careers, http))
    return sources


# --- körningar ---------------------------------------------------------------------------


class Runner:
    """Högst en körning åt gången i processen: "Sök nu", schemat och `run-once`."""

    def __init__(self, execute: Callable[[str], Run | None]) -> None:
        self._execute = execute
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Startar en manuell körning i en bakgrundstråd. Kastar `RunInProgress` direkt
        om en körning redan pågår."""
        self._acquire()
        try:
            thread = threading.Thread(
                target=self._guarded, args=("manual",), name="jobaplier-run", daemon=True
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
            return self._execute(trigger)
        finally:
            self._lock.release()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _acquire(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise RunInProgress("En körning pågår redan")

    def _guarded(self, trigger: str) -> None:
        """Kör med låset redan taget och släpper det; fel loggas i stället för att kastas."""
        try:
            run = self._execute(trigger)
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
    gmail: Gmail,
    trigger: str,
) -> Run:
    """En körning med aktuella inställningar. Saknas något som krävs innan källorna
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
    sources = build_sources(config, paths, http, gmail=gmail)
    return run_pipeline(
        engine, sources, config.preferences, ai, paths.cv, trigger=trigger  # type: ignore[arg-type]
    )


# --- startsvep -----------------------------------------------------------------------------


def sweep_interrupted_emails(engine: Engine) -> int:
    """Mejl som var mitt i utskick när processen dog: läget är okänt, så jobbet blir
    misslyckat (aldrig skickbart igen utan att användaren kontrollerat Gmail).
    Formulär i "skickas" väntar på användaren och lämnas orörda."""
    with session_scope(engine) as session:
        rows = session.exec(
            select(Job, Application)
            .join(Application, Application.job_id == Job.id)  # type: ignore[arg-type]
            .where(
                Job.status == JobStatus.submitting,
                Application.channel == Channel.email,
                Application.status == ApplicationStatus.submitting,
            )
        ).all()
        for job, app in rows:
            transition(job, JobStatus.failed)
            app.status = ApplicationStatus.failed
            app.error = EMAIL_INTERRUPTED
        return len(rows)


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
    if count := sweep_interrupted_emails(engine):
        log.warning("%d mejlansökningar avbröts under utskick och markerades misslyckade", count)
    if count := sweep_interrupted_drafts(engine):
        log.warning("%d brev avbröts medan de skrevs; jobben är poängsatta igen", count)
    if count := sweep_unfinished_runs(engine):
        log.warning("%d körningar avbröts och markerades som avslutade", count)


# --- koppling --------------------------------------------------------------------------------


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
    gmail = LazyGmail.from_paths(paths)
    http = make_http_client()

    email_applier = EmailApplier(gmail, holder.get, paths, lambda: emails_sent_today(engine))
    form_applier = FormApplier(holder.get, paths, default_adapters())
    # En enda instans per process: dess lås garanterar att ett jobb skickas högst en gång.
    service = ApplyService(
        engine,
        ai,
        paths,
        holder.get,
        {ApplyMethod.email: email_applier, ApplyMethod.form: form_applier},
    )

    runner = Runner(lambda trigger: execute_run(engine, paths, holder, ai, http, gmail, trigger))
    config = holder.current()
    times = config.schedule if config is not None else DEFAULT_SCHEDULE
    scheduler = build_scheduler(runner.run_scheduled, times)

    def on_config_saved(new: Config) -> None:
        holder.set(new)
        reschedule(scheduler, runner.run_scheduled, new.schedule)

    state = AppState(
        engine=engine,
        paths=paths,
        config=holder.get,
        service=service,
        run_now=runner.start,
        setup_missing=lambda: compute_setup_missing(paths, holder.current, anthropic_api_key),
        on_config_saved=on_config_saved,
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
