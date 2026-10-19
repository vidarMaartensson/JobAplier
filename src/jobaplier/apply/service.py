"""Flödet från poängsatt jobb till ansökan: länk, utkast, godkännande och skick.

Varje publik metod öppnar en egen transaktion och returnerar laddade, frånkopplade
objekt. Statuskontroll och statusbyte sker under tjänstens lås, så att två samtidiga
anrop för samma jobb aldrig båda går vidare (ett dubbelklick på "Godkänn och skicka"
startar formuläret bara en gång). AI- och appliernanrop görs utan lås och utan öppen session.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, select

from jobaplier.ai import AIClient, AIError
from jobaplier.apply.base import Applier, SubmitError, SubmitResult
from jobaplier.apply.letter import LetterResult, load_examples, write_letter
from jobaplier.apply.package import tailored_record
from jobaplier.apply.tailor import TailoredCV, tailor_cv, verify_tailored
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
    TailoredCVRecord,
    utcnow,
)
from jobaplier.profile.cv import CVMissing, CVUnreadable, cv_file_hash, load_or_build_profile
from jobaplier.profile.cv_document import load_or_build_cv_document
from jobaplier.status import transition

log = logging.getLogger(__name__)

S = JobStatus
MANUAL_EDIT = "manuell redigering"
CV_REPLACED_WARNING = (
    "Ditt CV har bytts sedan utkastet skapades – klicka Försök igen för ett nytt "
    "skräddarsytt CV"
)

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


class StaleDraft(AlreadyHandled):
    """Utkastet som sidan visade är inte längre den senaste versionen (gammal flik)."""


STALE_DRAFT = "En nyare version av utkastet finns – ladda om sidan"


class NoAutoApply(Exception):
    """Jobbet saknar automatisk ansökningsväg (bara formulär finns kvar)."""


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


def _last_instruction(session: Session, job_id: int) -> str | None:
    """Senaste instruktionen som gavs till AI:n (manuella redigeringar räknas inte)."""
    drafts = session.exec(
        select(Draft)
        .where(Draft.job_id == job_id)
        .order_by(Draft.version.desc(), Draft.id.desc())  # type: ignore[union-attr]
    )
    for draft in drafts:
        if draft.instruction != MANUAL_EDIT:
            return draft.instruction
    return None


def _application(session: Session, job_id: int, channel: Channel) -> Application | None:
    return session.exec(
        select(Application).where(Application.job_id == job_id, Application.channel == channel)
    ).first()


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
                current = _get_job(session, job_id)
                draft = self._add_draft(
                    session,
                    job_id,
                    letter_text=result.letter,
                    language=result.language,
                    unsupported=result.unsupported_claims,
                    instruction=None,
                )
                transition(current, S.draft_ready)
                draft = _detach(session, draft)
        except Exception:
            # AI-fel, saknat CV m.m.: jobbet får inte fastna i "utkast skrivs".
            with self._lock, session_scope(self.engine) as session:
                current = _get_job(session, job_id)
                if current.status == S.drafting:
                    transition(current, S.scored)
            raise
        # Brevet är sparat och jobbet klart; ett misslyckat CV ger bara reserven.
        self._tailor_or_log(job, draft.id, None)
        return draft

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
            draft = _detach(session, draft)
        self._tailor_or_log(job, draft.id, instruction)
        return draft

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
            session.flush()
            # Brevet redigerades för hand; CV:t är detsamma som i föregående version.
            previous = tailored_record(session, latest.id) if latest is not None else None
            if previous is not None:
                assert draft.id is not None
                session.add(
                    TailoredCVRecord(
                        draft_id=draft.id,
                        cv_hash=previous.cv_hash,
                        tailored_json=dict(previous.tailored_json),
                        warnings_json=list(previous.warnings_json),
                    )
                )
            return _detach(session, draft)

    def latest_draft(self, job_id: int) -> Draft | None:
        with session_scope(self.engine) as session:
            _get_job(session, job_id)
            draft = _latest_draft(session, job_id)
            return _detach(session, draft) if draft is not None else None

    # --- skräddarsytt CV -----------------------------------------------------------------

    def retry_tailor(self, job_id: int) -> Draft:
        """Gör om CV-anpassningen för senaste versionen. Fel kastas vidare till anroparen."""
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            _require(job, S.draft_ready)
            latest = _latest_draft(session, job_id)
            if latest is None:
                raise LookupError("Utkastet finns inte")
            instruction = _last_instruction(session, job_id)
            job = _detach(session, job)
            draft = _detach(session, latest)
        assert draft.id is not None
        self._tailor(job, draft.id, instruction)
        return draft

    def tailored_for(self, draft_id: int) -> tuple[TailoredCV, list[str]] | None:
        with session_scope(self.engine) as session:
            record = tailored_record(session, draft_id)
            if record is None:
                return None
            tailored = TailoredCV.model_validate(record.tailored_json)
            warnings = list(record.warnings_json)
            cv_hash = record.cv_hash
        try:
            replaced = cv_hash != cv_file_hash(self.paths.cv)
        except (CVMissing, CVUnreadable):
            replaced = False  # nedladdningen visar då själv felet om CV-filen
        if replaced:
            # Anpassningens varningar gäller det gamla CV:t, som inte längre används.
            warnings = [CV_REPLACED_WARNING]
        return tailored, warnings

    def _tailor_or_log(self, job: Job, draft_id: int, instruction: str | None) -> None:
        """Brevet är redan sparat: inget fel i anpassningen får göra *Ansök* till ett fel."""
        try:
            self._tailor(job, draft_id, instruction)
        except (AIError, CVMissing, CVUnreadable) as e:
            log.warning("CV:t kunde inte anpassas för utkast %s: %s", draft_id, e)
        except Exception:
            log.exception("CV:t kunde inte anpassas för utkast %s (oväntat fel)", draft_id)

    def _tailor(self, job: Job, draft_id: int, instruction: str | None) -> None:
        """Anpassar CV:t (AI-anrop utan lås) och ersätter utkastets `tailored_cvs`-rad."""
        # Hashen tas före dokumentet: byts filen emellan märks anpassningen som inaktuell.
        cv_hash = cv_file_hash(self.paths.cv)
        with session_scope(self.engine) as session:
            doc = load_or_build_cv_document(session, self.paths.cv, self.ai)
        tailored = tailor_cv(job, doc, self.ai, instruction=instruction).value
        _, warnings = verify_tailored(tailored, doc)
        with self._lock, session_scope(self.engine) as session:
            existing = tailored_record(session, draft_id)
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(
                TailoredCVRecord(
                    draft_id=draft_id,
                    cv_hash=cv_hash,
                    tailored_json=tailored.model_dump(mode="json"),
                    warnings_json=warnings,
                )
            )

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
        """`expected_channel` är kanalen som användaren såg (formulär). Har den
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
        except SubmitError as e:
            return self._finish(job_id, app_id, S.failed, ApplicationStatus.failed, error=str(e))
        except Exception as e:  # oväntat fel i appliern: läget måste ändå sparas
            # Webbläsarkrasch: formulärvägen skickar aldrig själv, så utkastet
            # kan godkännas igen.
            return self._finish(
                job_id,
                app_id,
                S.draft_ready,
                ApplicationStatus.failed,
                error=f"Formuläret kunde inte fyllas i: {type(e).__name__}: {e}",
            )
        return self._record(job_id, app_id, result)

    def _record(self, job_id: int, app_id: int, result: SubmitResult) -> Application:
        if result.status == ApplicationStatus.submitted:
            return self._finish(
                job_id,
                app_id,
                S.submitted,
                ApplicationStatus.submitted,
                receipt=result.receipt,
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
                error=notes,
            )
        return self._finish(
            job_id,
            app_id,
            S.failed,
            ApplicationStatus.failed,
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
                app = _application(session, job_id, Channel.form)
                if app is None:
                    raise LookupError("Ansökan finns inte")
            else:
                raise _already_handled(job)
            app.status = ApplicationStatus.submitted
            app.submitted_at = utcnow()
            transition(job, S.submitted)
            return _detach(session, app)

    def mark_sent_manually(self, job_id: int, draft_id: int) -> Application:
        """Användaren har själv skickat ansökan (mejl, arbetsgivarens sida eller formuläret
        utan automatisk ifyllning). Formulärjobb får kanalen `manual`."""
        with self._lock, session_scope(self.engine) as session:
            job = _get_job(session, job_id)
            draft = session.get(Draft, draft_id)
            if draft is None or draft.job_id != job_id:
                raise LookupError("Utkastet finns inte")
            _require(job, S.draft_ready)
            latest = _latest_draft(session, job_id)
            if latest is None or latest.id != draft_id:
                raise StaleDraft(STALE_DRAFT)
            channel = Channel.email if job.apply_method == ApplyMethod.email else Channel.manual
            # En äldre rad för samma kanal (t.ex. från tidigare mejlutskick) återanvänds.
            app = _application(session, job_id, channel)
            if app is None:
                app = Application(job_id=job_id, channel=channel)
                session.add(app)
            app.draft_id = draft_id
            app.status = ApplicationStatus.submitted
            app.receipt = None
            app.error = None
            app.dry_run = False
            app.submitted_at = utcnow()
            transition(job, S.submitted)
            return _detach(session, app)
