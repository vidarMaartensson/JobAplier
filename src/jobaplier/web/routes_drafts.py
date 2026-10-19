"""Ansökningspaketet: granska utkast, spara/ny version, nedladdningar, godkänn (formulär),
"Jag har skickat", markera som skickad och nytt försök."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlmodel import Session, col, select
from starlette.background import BackgroundTask

from jobaplier.ai import AIError
from jobaplier.apply.letter import word_count
from jobaplier.apply.package import (
    application_subject,
    cv_filename,
    email_body,
    letter_filename,
    mailto_link,
)
from jobaplier.apply.pdf import render_letter_pdf
from jobaplier.apply.service import (
    CV_REPLACED_WARNING,
    AlreadyHandled,
    ChannelChanged,
    STALE_DRAFT,
    NoAutoApply,
    StaleDraft,
)
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobStatus,
)
from jobaplier.profile.cv import CVMissing, CVUnreadable
from jobaplier.web.routes_jobs import _act, _is_htmx, _not_found, _render, _state

log = logging.getLogger(__name__)

router = APIRouter()

FLASH = {
    "saved": "Sparat – det här är en ny version av brevet",
    "regenerated": "Ny version skapad",
    "sent": "Markerad som skickad",
    "tailored": "Det skräddarsydda CV:t är klart",
}
NO_DRAFT = "Det finns inget utkast för det här jobbet"
ALREADY_HANDLED = "Ansökan är redan hanterad"
EMPTY_LETTER = "Brevet får inte vara tomt"
CHANNEL_CHANGED = "Ansökningsvägen har ändrats"
STALE_CHANNEL = (
    "Ansökningsvägen har ändrats sedan sidan laddades. Inget har skickats – öppna utkastet "
    "igen och kontrollera."
)
NO_INSTRUCTION = "Skriv en instruktion för den nya versionen, till exempel ”gör det kortare”"
UNANSWERED_PREFIX = "Obesvarade frågor: "
# Jobbstatusar där utkastsidan visar utfallet av ansökan i stället för paketet. En skickad
# ansökan visar paketet (skrivskyddat), så att filerna går att ladda ner igen.
OUTCOME_STATUSES = (JobStatus.submitting, JobStatus.failed)
PDF_FAILED = "PDF:en kunde inte skapas"
PDF_FAILED_TEXT = (
    "Något gick fel när PDF:en skapades. Försök igen om en stund – detaljerna finns i loggen."
)
TAILOR_FAILED = "Kunde inte skapa det skräddarsydda CV:t"
STALE_HEADING = "Utkastet har ändrats"


def _normalize(text: str) -> str:
    """Webbläsare skickar radbrytningar i textrutor som \\r\\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unanswered(error: str | None) -> list[str]:
    if not error or not error.startswith(UNANSWERED_PREFIX):
        return []
    return [q for q in error.removeprefix(UNANSWERED_PREFIX).split(", ") if q]


def _load(state, job_id: int) -> tuple[Job, Draft | None, Application | None]:
    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise _not_found()
        draft = session.exec(
            select(Draft)
            .where(Draft.job_id == job_id)
            .order_by(col(Draft.version).desc(), col(Draft.id).desc())
        ).first()
        application = session.exec(
            select(Application).where(Application.job_id == job_id).order_by(col(Application.id).desc())
        ).first()
        return job, draft, application


# --- utkastsidan ----------------------------------------------------------------------------


def _draft_page(
    request: Request,
    job_id: int,
    *,
    error: str | None = None,
    flash: str = "",
    letter_text: str | None = None,
    instruction: str = "",
    status_code: int = 200,
) -> Response:
    state = _state(request)
    job, draft, application = _load(state, job_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    if job.status in OUTCOME_STATUSES and application is not None:
        return _outcome(request, job_id, status_code=status_code, problem=error)
    text = draft.letter_text if letter_text is None else letter_text
    previous_error = (
        application.error
        if application is not None and application.status == ApplicationStatus.failed
        else None
    )
    sent = (
        application
        if job.status == JobStatus.submitted
        and application is not None
        and application.status == ApplicationStatus.submitted
        else None
    )
    return _render(
        request,
        "draft.html",
        {
            "job": job,
            "draft": draft,
            "letter_text": text,
            "words": word_count(text),
            "editable": job.status == JobStatus.draft_ready,
            "error": error,
            # En skickad ansökan har en egen ruta med datum; samma besked två gånger behövs inte.
            "flash": None if sent is not None and flash == "sent" else FLASH.get(flash),
            "instruction": instruction,
            "previous_error": previous_error,
            "sent": sent,
            **_cv_context(state, draft),
            **_email_context(state, job, draft),
        },
        status_code=status_code,
    )


def _cv_context(state, draft: Draft) -> dict:
    """Skräddarsytt CV: varningar, reserv och om *Försök igen* ska visas.

    Faktavarningarna för det strukturerade CV:t visas bara när nedladdningen bygger på
    det (inte när den ger original-PDF:en)."""
    assert draft.id is not None
    tailored = state.service.tailored_for(draft.id)
    warnings = state.cv_warnings() + tailored[1] if tailored is not None else []
    return {
        "cv_fallback": tailored is None,
        "cv_warnings": warnings,
        "cv_retry": tailored is None or CV_REPLACED_WARNING in warnings,
    }


def _email_context(state, job: Job, draft: Draft) -> dict:
    """Mottagare, ämne, brödtext och `mailto:`-länk för mejljobb."""
    if job.apply_method != ApplyMethod.email:
        return {}
    recipient = (job.apply_email or "").strip()
    subject = application_subject(job)
    body = email_body(job, draft, state.config())
    return {
        "email": {
            "to": recipient,
            "subject": subject,
            "body": body,
            # Byggs bara av `mailto_link` (som kontrollerar adressen) och går därför
            # inte genom `safe_url`, som bara släpper igenom http(s).
            "mailto": mailto_link(recipient, subject, body),
        }
    }


@router.get("/jobs/{job_id}/draft")
def draft_page(request: Request, job_id: int, msg: str = "") -> Response:
    return _draft_page(request, job_id, flash=msg)


# --- nedladdningar --------------------------------------------------------------------------


def _download_error(
    request: Request, job_id: int, heading: str, message: str, status_code: int
) -> Response:
    return _render(
        request,
        "error.html",
        {
            "heading": heading,
            "message": message,
            "setup_link": status_code == 404,
            "back_url": f"/jobs/{job_id}/draft",
        },
        status_code=status_code,
    )


def _download_target(request: Request, job_id: int) -> tuple[Job, Draft]:
    job, draft, _application = _load(_state(request), job_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=NO_DRAFT)
    return job, draft


def _applicant_name(state) -> str:
    """Namnet i CV-filens namn: inställningarna, annars CV:ts namn (`cv_filename` tar
    till "CV" om inget finns)."""
    name = state.config().profile.name.strip()
    if name:
        return name
    document = state.cv_document()
    return document.name if document is not None else ""


def _send_pdf(path: Path, filename: str, tmp: tempfile.TemporaryDirectory) -> FileResponse:
    """Den tillfälliga katalogen tas bort när svaret har skickats."""
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        background=BackgroundTask(tmp.cleanup),
    )


@router.get("/jobs/{job_id}/draft/cv.pdf")
def download_cv(request: Request, job_id: int) -> Response:
    state = _state(request)
    job, draft = _download_target(request, job_id)
    assert draft.id is not None
    tmp = tempfile.TemporaryDirectory(prefix="jobaplier-cv-")
    try:
        # Utan skräddarsytt CV är `path` originalet `paths.cv`; det skickas som det är.
        path, _tailored = state.cv_pdf(draft.id, Path(tmp.name) / "cv.pdf")
        filename = cv_filename(_applicant_name(state), job.company)
    except CVMissing as exc:
        tmp.cleanup()
        return _download_error(request, job_id, "CV:t saknas", str(exc), 404)
    except Exception:
        tmp.cleanup()
        log.exception("CV-PDF:en för utkast %s kunde inte skapas", draft.id)
        return _download_error(request, job_id, PDF_FAILED, PDF_FAILED_TEXT, 500)
    return _send_pdf(path, filename, tmp)


@router.get("/jobs/{job_id}/draft/letter.pdf")
def download_letter(request: Request, job_id: int) -> Response:
    """Senast sparade versionen (används när ansökan redan är skickad)."""
    job, draft = _download_target(request, job_id)
    return _letter_pdf(request, job, draft.letter_text, draft.id)


@router.post("/jobs/{job_id}/draft/letter.pdf")
def download_edited_letter(
    request: Request,
    job_id: int,
    draft_id: int = Form(...),
    letter_text: str = Form(""),
) -> Response:
    """Brevet exakt som det står i textrutan just nu. Inget sparas (texten sparas först vid
    *Jag har skickat*). Går utkastet inte längre att ändra används den sparade versionen."""
    job, draft = _posted_draft(request, job_id, draft_id)
    if job.status != JobStatus.draft_ready:
        return _letter_pdf(request, job, draft.letter_text, draft.id)
    text = _normalize(letter_text)
    if not text.strip():
        return _draft_page(request, job_id, error=EMPTY_LETTER, letter_text=text, status_code=400)
    return _letter_pdf(request, job, text, draft.id)


def _posted_draft(request: Request, job_id: int, draft_id: int) -> tuple[Job, Draft]:
    with Session(_state(request).engine) as session:
        job = session.get(Job, job_id)
        draft = session.get(Draft, draft_id)
        if job is None or draft is None or draft.job_id != job_id:
            raise HTTPException(status_code=404, detail="Utkastet finns inte")
        session.expunge_all()
        return job, draft


def _letter_pdf(request: Request, job: Job, text: str, draft_id: int | None) -> Response:
    assert job.id is not None
    tmp = tempfile.TemporaryDirectory(prefix="jobaplier-brev-")
    try:
        path = render_letter_pdf(
            text, _state(request).config().profile.name, Path(tmp.name) / "brev.pdf"
        )
    except Exception:
        tmp.cleanup()
        log.exception("Brevets PDF för utkast %s kunde inte skapas", draft_id)
        return _download_error(request, job.id, PDF_FAILED, PDF_FAILED_TEXT, 500)
    return _send_pdf(path, letter_filename(job.company), tmp)


def _posted_version(
    request: Request, job_id: int, draft_id: int, letter_text: str | None
) -> Draft:
    """Versionen som *Jag har skickat* ska markera: den som motsvarar textrutan.

    - Ingen text (eller jobbet går inte längre att ändra): versionen `draft_id`, som den är
      sparad; ändringar i rutan räknas inte.
    - Samma text som senaste versionen: den (t.ex. om texten redan sparats från en annan
      flik och sidan har kvar ett äldre `draft_id`).
    - Annars måste `draft_id` vara senaste versionen (annars `StaleDraft`, gammal flik),
      och texten sparas som en ny version (`ValueError` om den är tom).
    """
    state = _state(request)
    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        posted = session.get(Draft, draft_id)
        if job is None or posted is None or posted.job_id != job_id:
            raise LookupError("Utkastet finns inte")
        session.expunge(posted)
    latest = state.service.latest_draft(job_id)
    assert latest is not None
    if letter_text is None or job.status != JobStatus.draft_ready:
        return posted
    text = _normalize(letter_text)
    if text == latest.letter_text:
        return latest
    if posted.id != latest.id:
        raise StaleDraft(STALE_DRAFT)
    return state.service.save_edit(job_id, text)


# --- utfallssidan (submitted.html) --------------------------------------------------------


def _outcome(
    request: Request,
    job_id: int,
    *,
    application: Application | None = None,
    problem: str | None = None,
    heading: str | None = None,
    status_code: int = 200,
) -> Response:
    state = _state(request)
    job, _draft, latest = _load(state, job_id)
    application = application or latest
    sent_draft = None
    if application is not None and application.draft_id is not None:
        with Session(state.engine) as session:
            sent_draft = session.get(Draft, application.draft_id)
    return _render(
        request,
        "submitted.html",
        {
            "job": job,
            "application": application,
            "problem": problem,
            "heading": heading,
            "unanswered": _unanswered(application.error if application else None),
            "sent_draft": sent_draft,
            "manual": application is not None and application.channel == Channel.manual,
        },
        status_code=status_code,
    )


def _stale(request: Request, job_id: int, exc: Exception) -> Response:
    """Sidan visade en äldre version av utkastet; inget har ändrats eller markerats."""
    return _outcome(request, job_id, problem=str(exc), heading=STALE_HEADING, status_code=409)


def _already_handled(request: Request, job_id: int, exc: Exception) -> Response:
    return _outcome(
        request, job_id, problem=f"{ALREADY_HANDLED}. {exc}", heading=ALREADY_HANDLED, status_code=409
    )


# --- spara och ny version ------------------------------------------------------------------


@router.post("/jobs/{job_id}/draft")
def save_edit(request: Request, job_id: int, letter_text: str = Form("")) -> Response:
    text = _normalize(letter_text)
    try:
        _state(request).service.save_edit(job_id, text)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    except ValueError:
        return _draft_page(
            request, job_id, error=EMPTY_LETTER, letter_text=text, status_code=400
        )
    return RedirectResponse(f"/jobs/{job_id}/draft?msg=saved", status_code=303)


@router.post("/jobs/{job_id}/draft/regenerate")
def regenerate(request: Request, job_id: int, instruction: str = Form("")) -> Response:
    instruction = instruction.strip()
    if not instruction:
        return _draft_page(request, job_id, error=NO_INSTRUCTION, status_code=400)
    try:
        _state(request).service.regenerate(job_id, instruction)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    except (AIError, CVMissing, CVUnreadable) as exc:
        return _draft_page(
            request,
            job_id,
            error=f"Kunde inte skriva en ny version: {exc}",
            instruction=instruction,
            status_code=502,
        )
    return RedirectResponse(f"/jobs/{job_id}/draft?msg=regenerated", status_code=303)


# --- godkänn och skicka ---------------------------------------------------------------------


@router.post("/jobs/{job_id}/draft/approve")
def approve(
    request: Request,
    job_id: int,
    draft_id: int = Form(...),
    letter_text: str | None = Form(None),
    channel: str = Form(""),
) -> Response:
    """`channel` är kanalen som sidan visade (mejl eller formulär). Skiljer den sig från
    jobbets nuvarande kanal skickas inget."""
    state = _state(request)
    try:
        expected = Channel(channel) if channel else None
    except ValueError:
        return _channel_changed(request, job_id, STALE_CHANNEL)
    try:
        if letter_text is not None:
            draft_id = _save_unsaved_edit(request, job_id, draft_id, _normalize(letter_text))
        application = state.service.approve_and_submit(
            job_id, draft_id, expected_channel=expected
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ChannelChanged as exc:
        return _channel_changed(request, job_id, str(exc))
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    except NoAutoApply as exc:
        return _outcome(request, job_id, problem=str(exc), heading="Kan inte skicka", status_code=409)
    except ValueError:
        return _draft_page(
            request, job_id, error=EMPTY_LETTER, letter_text=_normalize(letter_text or ""), status_code=400
        )
    return _outcome(request, job_id, application=application)


def _channel_changed(request: Request, job_id: int, message: str) -> Response:
    return _outcome(request, job_id, problem=message, heading=CHANNEL_CHANGED, status_code=409)


def _save_unsaved_edit(request: Request, job_id: int, draft_id: int, text: str) -> int:
    """Ändringar i textrutan som inte sparats ännu blir en ny version innan brevet skickas,
    så att det som skickas alltid är det användaren ser."""
    state = _state(request)
    with Session(state.engine) as session:
        draft = session.get(Draft, draft_id)
        if draft is None or draft.job_id != job_id:
            raise LookupError("Utkastet finns inte")
        unchanged = draft.letter_text == text
    if unchanged:
        return draft_id
    return state.service.save_edit(job_id, text).id


# --- jag har skickat och nytt skräddarsytt CV -------------------------------------------


@router.post("/jobs/{job_id}/draft/sent")
def mark_sent(
    request: Request,
    job_id: int,
    draft_id: int = Form(...),
    letter_text: str | None = Form(None),
) -> Response:
    """*Jag har skickat*: användaren har skickat ansökan själv (mejl, annonsen eller
    formuläret utan automatisk ifyllning). `draft_id` är
    versionen som sidan visade; en äldre version (gammal flik) ger 409. Osparade ändringar
    i textrutan sparas först, och det är den versionen som markeras som skickad."""
    try:
        draft = _posted_version(request, job_id, draft_id, letter_text)
        assert draft.id is not None
        _state(request).service.mark_sent_manually(job_id, draft.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except StaleDraft as exc:
        return _stale(request, job_id, exc)
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    except ValueError:
        return _draft_page(
            request, job_id, error=EMPTY_LETTER, letter_text=_normalize(letter_text or ""),
            status_code=400,
        )
    return RedirectResponse(f"/jobs/{job_id}/draft?msg=sent", status_code=303)


@router.post("/jobs/{job_id}/draft/tailor")
def retry_tailor(request: Request, job_id: int) -> Response:
    try:
        _state(request).service.retry_tailor(job_id)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    except (AIError, CVMissing, CVUnreadable) as exc:
        return _draft_page(request, job_id, error=f"{TAILOR_FAILED}: {exc}", status_code=502)
    return RedirectResponse(f"/jobs/{job_id}/draft?msg=tailored", status_code=303)


# --- markera som skickad och nytt försök -------------------------------------------------


@router.post("/jobs/{job_id}/mark-submitted")
def mark_submitted(request: Request, job_id: int) -> Response:
    """Från jobblistan (HTMX) byts raden ut; annars visas utfallssidan."""
    if _is_htmx(request):
        return _act(request, job_id, _state(request).service.mark_submitted)
    try:
        application = _state(request).service.mark_submitted(job_id)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    return _outcome(request, job_id, application=application, heading="Markerad som skickad")


@router.post("/jobs/{job_id}/retry")
def retry(request: Request, job_id: int) -> Response:
    try:
        _state(request).service.retry(job_id)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _already_handled(request, job_id, exc)
    return RedirectResponse(f"/jobs/{job_id}/draft", status_code=303)
