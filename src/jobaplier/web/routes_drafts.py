"""Granska utkast, spara/ny version, godkänn och skicka, markera som skickad och nytt försök."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, col, select

from jobaplier.ai import AIError
from jobaplier.apply.letter import word_count
from jobaplier.apply.service import AlreadyHandled, ChannelChanged, NoAutoApply
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

router = APIRouter()

FLASH = {
    "saved": "Sparat – det här är en ny version av brevet",
    "regenerated": "Ny version skapad",
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
# Jobbstatusar där utkastsidan visar utfallet av ansökan i stället för redigeraren.
OUTCOME_STATUSES = (JobStatus.submitting, JobStatus.submitted, JobStatus.failed)


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
    return _render(
        request,
        "draft.html",
        {
            "job": job,
            "draft": draft,
            "letter_text": text,
            "words": word_count(text),
            "editable": job.status == JobStatus.draft_ready,
            "dry_run": state.config().apply.dry_run and job.apply_method == ApplyMethod.email,
            "error": error,
            "flash": FLASH.get(flash),
            "instruction": instruction,
            "previous_error": previous_error,
        },
        status_code=status_code,
    )


@router.get("/jobs/{job_id}/draft")
def draft_page(request: Request, job_id: int, msg: str = "") -> Response:
    return _draft_page(request, job_id, flash=msg)


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
            "own_email": state.config().profile.email,
            "unanswered": _unanswered(application.error if application else None),
            "sent_draft": sent_draft,
            "manual": application is not None and application.channel == Channel.manual,
        },
        status_code=status_code,
    )


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
