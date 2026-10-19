"""Flödet från poängsatt jobb till ansökan: länk, utkast, godkännande och skick.

Varje publik metod öppnar en egen transaktion och returnerar laddade, frånkopplade
objekt. Statuskontroll och statusbyte sker under tjänstens lås, så att två samtidiga
anrop för samma jobb aldrig båda går vidare (ett dubbelklick på "Godkänn och skicka"
ger exakt ett mejl). AI- och appliernanrop görs utan lås och utan öppen session.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func
from sqlmodel import Session, SQLModel, select

from jobaplier.ai import AIClient
from jobaplier.apply.base import AmbiguousSendError, Applier, SubmitError, SubmitResult
from jobaplier.apply.letter import LetterResult, load_examples, write_letter
from jobaplier.config import Config, Paths
from jobaplier.db import session_scope
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobStatus,
    utcnow,
)
from jobaplier.profile.cv import load_or_build_profile
from jobaplier.status import transition

S = JobStatus
LOCAL_TZ = ZoneInfo("Europe/Stockholm")
MANUAL_EDIT = "manuell redigering"
CHECK_GMAIL = "Kontrollera Skickat i Gmail innan du försöker igen"

STATUS_LABELS: dict[JobStatus, str] = {
    S.new: "ny",
    S.filtered: "bortfiltrerad",
    S.scored: "poängsatt",
    S.dismissed: "avfärdad",
    S.link: "ansök via länk",
    S.drafting: "utkast skrivs",
    S.draft_ready: "utkast klart",
    S.approved: "godkänd",
    S.submitting: "skickas",
    S.submitted: "skickad",
    S.failed: "misslyckad",
}

_CHANNELS: dict[ApplyMethod, Channel] = {
    ApplyMethod.email: Channel.email,
    ApplyMethod.form: Channel.form,
}
CHANNEL_LABELS: dict[Channel, str] = {
    Channel.email: "mejl",
    Channel.form: "formulär",
    Channel.manual: "egen ansökan",
}


class AlreadyHandled(Exception):
    """Jobbet har inte (längre) den status som åtgärden kräver."""


class ChannelChanged(AlreadyHandled):
    """Ansökningsvägen är inte längre den som användaren såg när utkastet visades."""


class NoAutoApply(Exception):
    """Jobbet saknar automatisk ansökningsväg (mejl eller formulär)."""


def _detach[M: SQLModel](session: Session, obj: M) -> M:
    session.flush()
    session.refresh(obj)
    session.expunge(obj)
    return obj


def _get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError("Jobbet finns inte")
    return job


def _already_handled(job: Job) -> AlreadyHandled:
    return AlreadyHandled(
        f"Åtgärden går inte att utföra: jobbet har status ”{STATUS_LABELS[job.status]}”"
    )


def _require(job: Job, *allowed: JobStatus) -> None:
    if job.status not in allowed:
        raise _already_handled(job)


def _latest_draft(session: Session, job_id: int) -> Draft | None:
    return session.exec(
        select(Draft)
        .where(Draft.job_id == job_id)
        .order_by(Draft.version.desc(), Draft.id.desc())  # type: ignore[union-attr]
    ).first()


def _application(session: Session, job_id: int, channel: Channel) -> Application | None:
    return session.exec(
        select(Application).where(Application.job_id == job_id, Application.channel == channel)
    ).first()


def emails_sent_today(engine: Engine, now: datetime | None = None) -> int:
    """Skarpa (ej torrkörda) mejlansökningar skickade sedan lokal midnatt."""
    now = now or utcnow()
    local = now.replace(tzinfo=UTC).astimezone(LOCAL_TZ)
    midnight = (
        local.replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    with Session(engine) as session:
        return session.exec(
            select(func.count())
            .select_from(Application)
            .where(
                Application.channel == Channel.email,
                Application.status == ApplicationStatus.submitted,
                Application.dry_run.is_(False),  # type: ignore[attr-defined]
                Application.submitted_at >= midnight,  # type: ignore[operator]
            )
        ).one()


class ApplyService:
    def __init__(
        self,
        engine: Engine,
        ai: AIClient,
        paths: Paths,
        config: Callable[[], Config],
        appliers: dict[ApplyMethod, Applier],
    ) -> None:
        self.engine = engine
        self.ai = ai
        self.paths = paths
        self.config = config
        self.appliers = appliers
        self._lock = threading.Lock()

    # --- länk och avfärda ---------------------------------------------------------

    def mark_link(self, job_id: int) -> Job:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.scored)
            transition(job, S.link)
            return _detach(session, job)

    def dismiss(self, job_id: int) -> Job:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.scored, S.draft_ready)
            transition(job, S.dismissed)
            return _detach(session, job)

    def retry(self, job_id: int) -> Job:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.failed)
            transition(job, S.draft_ready)
            return _detach(session, job)

    # --- utkast -----------------------------------------------------------------------

    def start_draft(self, job_id: int) -> Draft:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.scored)
            transition(job, S.drafting)
            job = _detach(session, job)
        try:
            result = self._write(job, instruction=None, previous=None)
            with self._lock, session_scope(self.engine) as session:
                job = _get_job(session, job_id)
                draft = self._add_draft(
                    session,
                    job_id,
                    letter_text=result.letter,
                    language=result.language,
                    unsupported=result.unsupported_claims,
                    instruction=None,
                )
                transition(job, S.draft_ready)
                return _detach(session, draft)
        except Exception:
            # AI-fel, saknat CV m.m.: jobbet får inte fastna i "utkast skrivs".
            with self._lock, session_scope(self.engine) as session:
                job = _get_job(session, job_id)
                if job.status == S.drafting:
                    transition(job, S.scored)
            raise

    def regenerate(self, job_id: int, instruction: str) -> Draft:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.draft_ready)
            latest = _latest_draft(session, job_id)
            previous = latest.letter_text if latest is not None else None
            job = _detach(session, job)
        result = self._write(job, instruction=instruction, previous=previous)
        with self._lock, session_scope(self.engine) as session:
            _require(_get_job(session, job_id), S.draft_ready)
            draft = self._add_draft(
                session,
                job_id,
                letter_text=result.letter,
                language=result.language,
                unsupported=result.unsupported_claims,
                instruction=instruction,
            )
            return _detach(session, draft)

    def save_edit(self, job_id: int, letter_text: str) -> Draft:
        if not letter_text.strip():
            raise ValueError("Brevet får inte vara tomt")
        with self._lock, session_scope(self.engine) as session:
            _require(_get_job(session, job_id), S.draft_ready)
            latest = _latest_draft(session, job_id)
            draft = self._add_draft(
                session,
                job_id,
                letter_text=letter_text,
                language=latest.language if latest is not None else "sv",
                unsupported=[],
                instruction=MANUAL_EDIT,
            )
            return _detach(session, draft)

    def latest_draft(self, job_id: int) -> Draft | None:
        with session_scope(self.engine) as session:
            _get_job(session, job_id)
            draft = _latest_draft(session, job_id)
            return _detach(session, draft) if draft is not None else None

    def _write(self, job: Job, *, instruction: str | None, previous: str | None) -> LetterResult:
        with session_scope(self.engine) as session:
            profile = _detach(session, load_or_build_profile(session, self.paths.cv, self.ai))
        return write_letter(
            job,
            profile,
            self.ai,
            examples=load_examples(self.paths.letter_examples),
            instruction=instruction,
            previous=previous,
        ).value

    @staticmethod
    def _add_draft(
        session: Session,
        job_id: int,
        *,
        letter_text: str,
        language: str,
        unsupported: list[str],
        instruction: str | None,
    ) -> Draft:
        latest = _latest_draft(session, job_id)
        draft = Draft(
            job_id=job_id,
            version=latest.version + 1 if latest is not None else 1,
            letter_text=letter_text,
            language=language,
            unsupported_claims_json=list(unsupported),
            instruction=instruction,
        )
        session.add(draft)
        return draft

    # --- godkänn och skicka -------------------------------------------------------------

    def approve_and_submit(
        self, job_id: int, draft_id: int, *, expected_channel: Channel | None = None
    ) -> Application:
        """`expected_channel` är kanalen som användaren såg (mejl eller formulär). Har den
        ändrats sedan sidan laddades skickas inget och `ChannelChanged` kastas."""
        # 1. Reservera jobbet: statusbytet till "skickas" committas innan något skickas,
        #    så ett andra anrop (dubbelklick) ser "skickas" och avbryts.
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            draft = session.get(Draft, draft_id)
            if draft is None or draft.job_id != job_id:
                raise LookupError("Utkastet finns inte")
            _require(job, S.draft_ready)
            channel = _CHANNELS.get(job.apply_method)
            applier = self.appliers.get(job.apply_method)
            if channel is None or applier is None:
                raise NoAutoApply("Det här jobbet har ingen automatisk ansökningsväg")
            if expected_channel is not None and expected_channel != channel:
                raise ChannelChanged(
                    f"Ansökningsvägen har ändrats till {CHANNEL_LABELS[channel]} sedan sidan "
                    "laddades. Inget har skickats – öppna utkastet igen och kontrollera."
                )
            transition(job, S.approved)
            transition(job, S.submitting)
            app = _application(session, job_id, channel)
            if app is None:  # vid nytt försök återanvänds raden (unik per jobb och kanal)
                app = Application(job_id=job_id, channel=channel)
                session.add(app)
            app.draft_id = draft_id
            app.status = ApplicationStatus.submitting
            app.receipt = None
            app.error = None
            app.submitted_at = None
            session.flush()
            app_id = app.id
            job = _detach(session, job)
            draft = _detach(session, draft)
        assert app_id is not None

        # 2. Skicka utan lås och utan öppen session.
        try:
            result = applier.submit(job, draft)
        except AmbiguousSendError as e:
            # Kan ha skickats: misslyckad, och användaren varnas innan ett nytt försök.
            return self._finish(
                job_id, app_id, S.failed, ApplicationStatus.failed, error=f"{CHECK_GMAIL}: {e}"
            )
        except SubmitError as e:
            return self._finish(job_id, app_id, S.failed, ApplicationStatus.failed, error=str(e))
        except Exception as e:  # oväntat fel i appliern: läget måste ändå sparas
            if channel == Channel.form:
                # Webbläsarkrasch: formulärvägen skickar aldrig själv, så utkastet
                # kan godkännas igen.
                return self._finish(
                    job_id,
                    app_id,
                    S.draft_ready,
                    ApplicationStatus.failed,
                    error=f"Formuläret kunde inte fyllas i: {type(e).__name__}: {e}",
                )
            # Okänt läge för mejlet: misslyckad, aldrig tillbaka till ett skickbart läge.
            return self._finish(
                job_id,
                app_id,
                S.failed,
                ApplicationStatus.failed,
                error=(
                    f"Oväntat fel när ansökan skulle skickas: {type(e).__name__}: {e}. "
                    f"{CHECK_GMAIL}."
                ),
            )
        return self._record(job_id, app_id, result)

    def _record(self, job_id: int, app_id: int, result: SubmitResult) -> Application:
        if result.status == ApplicationStatus.submitted:
            # En torrkörning (mejlet gick till användaren själv) förbrukar inte jobbet:
            # det blir utkast igen och kan skickas på riktigt senare (samma rad återanvänds).
            return self._finish(
                job_id,
                app_id,
                S.draft_ready if result.dry_run else S.submitted,
                ApplicationStatus.submitted,
                receipt=result.receipt,
                dry_run=result.dry_run,
                submitted_at=utcnow(),
            )
        if result.status == ApplicationStatus.submitting:
            # Formulär ifyllt i webbläsaren; användaren granskar och skickar själv.
            notes = f"Obesvarade frågor: {', '.join(result.notes)}" if result.notes else None
            return self._finish(
                job_id,
                app_id,
                None,
                ApplicationStatus.submitting,
                receipt=result.receipt,
                dry_run=result.dry_run,
                error=notes,
            )
        return self._finish(
            job_id,
            app_id,
            S.failed,
            ApplicationStatus.failed,
            dry_run=result.dry_run,
            error="; ".join(result.notes) or "Ansökan kunde inte skickas",
        )

    def _finish(
        self,
        job_id: int,
        app_id: int,
        job_to: JobStatus | None,
        status: ApplicationStatus,
        *,
        receipt: str | None = None,
        dry_run: bool = False,
        error: str | None = None,
        submitted_at: datetime | None = None,
    ) -> Application:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            app = session.get(Application, app_id)
            assert app is not None
            # Har användaren hunnit markera ansökan som skickad under formulärifyllningen
            # skrivs den inte över.
            if job.status == S.submitting:
                if job_to is not None:
                    transition(job, job_to)
                app.status = status
                app.receipt = receipt
                app.dry_run = dry_run
                app.error = error
                app.submitted_at = submitted_at
            return _detach(session, app)

    def mark_submitted(self, job_id: int) -> Application:
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            if job.status == S.link:
                app = _application(session, job_id, Channel.manual)
                if app is None:
                    app = Application(job_id=job_id, channel=Channel.manual)
                    session.add(app)
                latest = _latest_draft(session, job_id)
                app.draft_id = latest.id if latest is not None else None
                app.error = None
            elif job.status == S.submitting and job.apply_method == ApplyMethod.form:
                # Mejl i "skickas" är ett pågående skick och markeras inte för hand.
                app = _application(session, job_id, Channel.form)
                if app is None:
                    raise LookupError("Ansökan finns inte")
            else:
                raise _already_handled(job)
            app.status = ApplicationStatus.submitted
            app.dry_run = False
            app.submitted_at = utcnow()
            transition(job, S.submitted)
            return _detach(session, app)
