"""Jobblista, jobbdetalj, åtgärder på jobb och "Sök nu"."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

from jobaplier.ai import AIError, monthly_spend
from jobaplier.apply.service import STATUS_LABELS, AlreadyHandled
from jobaplier.models import ApplyMethod, Job, JobSource, JobStatus, Run
from jobaplier.pipeline.run import RunInProgress
from jobaplier.profile.cv import CVMissing, CVUnreadable
from jobaplier.sources.linkedin_links import (
    MAX_LINKS,
    LinkedInLinksSource,
    TooManyLinks,
    parse_linkedin_links,
)
from jobaplier.web.state import AppState

router = APIRouter()

# Meddelanden i `?msg=` slås upp här. Fri text från adressen visas aldrig.
FLASH = {
    "started": "Sökning startad",
    "running": "En sökning pågår redan",
    "handled": "Åtgärden gick inte att utföra eftersom jobbet redan har ändrats",
    "no_letter": "Kunde inte skriva brevet",
}
# Jobb mitt i ansökningsflödet visas överst i standardlistan, oavsett tröskel.
ONGOING_STATUSES = (JobStatus.draft_ready, JobStatus.submitting, JobStatus.failed)
NOT_FOUND = "Jobbet finns inte"
IN_PROGRESS = "En sökning pågår redan – försök igen om en stund"
NO_LINKS = "Inga LinkedIn-jobblänkar hittades"
MAX_REJECTED_SHOWN = 10
APPLY_METHOD_LABELS = {
    ApplyMethod.email: "Mejl",
    ApplyMethod.form: "Formulär",
    ApplyMethod.unknown: "Okänd",
}


@dataclass
class RowView:
    job: Job
    sources: list[str]
    error: str | None = None


@dataclass
class Banners:
    warnings: dict[str, str] = field(default_factory=dict)
    ai_message: str | None = None


def _state(request: Request) -> AppState:
    return request.app.state.jobaplier


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _render(request: Request, name: str, context: dict, status_code: int = 200) -> Response:
    context = {
        "status_labels": STATUS_LABELS,
        "method_labels": APPLY_METHOD_LABELS,
        **context,
    }
    return _templates(request).TemplateResponse(request, name, context, status_code=status_code)


def _parse_status(value: str | None) -> JobStatus | None:
    try:
        return JobStatus(value) if value else None
    except ValueError:
        return None


def _source_names(session: Session, job_ids: list[int]) -> dict[int, list[str]]:
    names: dict[int, list[str]] = {job_id: [] for job_id in job_ids}
    if not job_ids:
        return names
    rows = session.exec(
        select(JobSource.job_id, JobSource.source)
        .where(col(JobSource.job_id).in_(job_ids))
        .order_by(JobSource.id)
    )
    for job_id, source in rows:
        if source not in names[job_id]:
            names[job_id].append(source)
    return names


def _from_source(source: str):
    return col(Job.id).in_(select(JobSource.job_id).where(JobSource.source == source))


def _banners(state: AppState) -> Banners:
    config = state.config()
    with Session(state.engine) as session:
        run = session.exec(
            select(Run).order_by(col(Run.started_at).desc(), col(Run.id).desc())
        ).first()
    warnings = dict(run.errors_json) if run is not None else {}
    ai_message = warnings.pop("budget", None)
    if (
        ai_message is None
        and config.ai.backend == "api"
        and monthly_spend(state.engine) >= config.ai_monthly_budget_usd
    ):
        ai_message = (
            f"Månadstaket för AI-kostnader ({config.ai_monthly_budget_usd:g} USD) är nått. "
            "Nya jobb poängsätts och brev skrivs först när taket höjs eller månaden är slut."
        )
    return Banners(warnings=warnings, ai_message=ai_message)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=NOT_FOUND)


# --- lista ----------------------------------------------------------------------------


def _links(count: int) -> str:
    return f"{count} länk" if count == 1 else f"{count} länkar"


def _import_flash(n: str, known: str, again: str) -> str | None:
    """`n` nya länkar, `known` som redan fanns, varav `again` ofullständiga som hämtas igen."""
    try:
        added, existing, refetched = int(n), int(known or "0"), int(again or "0")
    except ValueError:
        return None
    if min(added, existing, refetched) < 0 or refetched > existing:
        return None
    if not 1 <= added + existing <= MAX_LINKS:
        return None
    parts = []
    if added:
        parts.append(f"{_links(added)} lades till – poängsätts nu")
    if existing:
        part = f"{_links(existing)} fanns redan"
        if refetched:
            ads = "ofullständig annons" if refetched == 1 else "ofullständiga annonser"
            part += f" – {refetched} {ads} hämtas igen"
        parts.append(part)
    return ". ".join(parts)


def _flash(msg: str, n: str, known: str = "", again: str = "") -> str | None:
    if msg == "imported":
        return _import_flash(n, known, again)
    return FLASH.get(msg)


@dataclass
class ImportForm:
    """Det som visas under importrutan när den renderas om efter ett fel."""

    text: str = ""
    error: str | None = None
    rejected: list[str] = field(default_factory=list)

    @property
    def rejected_shown(self) -> list[str]:
        return self.rejected[:MAX_REJECTED_SHOWN]

    @property
    def rejected_more(self) -> int:
        return max(0, len(self.rejected) - MAX_REJECTED_SHOWN)


def _render_list(
    request: Request,
    *,
    more: str = "",
    status: str = "",
    source: str = "",
    flash: str | None = None,
    import_form: ImportForm | None = None,
    status_code: int = 200,
) -> Response:
    state = _state(request)
    threshold = state.config().scoring.threshold
    wanted = _parse_status(status)
    show_all = bool(more)

    query = select(Job)
    if wanted is None:
        query = query.where(Job.status == JobStatus.scored)
    else:
        query = query.where(Job.status == wanted)
    if (wanted is None or wanted == JobStatus.scored) and not show_all:
        query = query.where(col(Job.score) >= threshold)
    if source:
        query = query.where(_from_source(source))
    query = query.order_by(col(Job.score).desc(), col(Job.id))

    ongoing_query = None
    if wanted is None:
        ongoing_query = select(Job).where(col(Job.status).in_(ONGOING_STATUSES))
        if source:
            ongoing_query = ongoing_query.where(_from_source(source))
        ongoing_query = ongoing_query.order_by(col(Job.updated_at).desc(), col(Job.id).desc())

    with Session(state.engine) as session:
        jobs = list(session.exec(query))
        ongoing_jobs = list(session.exec(ongoing_query)) if ongoing_query is not None else []
        names = _source_names(session, [j.id for j in jobs + ongoing_jobs])
        all_sources = list(session.exec(select(JobSource.source).distinct().order_by(JobSource.source)))
    rows = [RowView(job=j, sources=names[j.id]) for j in jobs]
    ongoing = [RowView(job=j, sources=names[j.id]) for j in ongoing_jobs]

    return _render(
        request,
        "jobs.html",
        {
            "rows": rows,
            "ongoing": ongoing,
            "banners": _banners(state),
            "flash": flash,
            "import_form": import_form or ImportForm(),
            "max_links": MAX_LINKS,
            "threshold": threshold,
            "more": show_all,
            "status": wanted.value if wanted else "",
            "source": source,
            "all_sources": all_sources,
            "all_statuses": list(JobStatus),
        },
        status_code=status_code,
    )


@router.get("/")
def job_list(
    request: Request,
    more: str = "",
    status: str = "",
    source: str = "",
    msg: str = "",
    n: str = "",
    known: str = "",
    again: str = "",
) -> Response:
    return _render_list(
        request, more=more, status=status, source=source, flash=_flash(msg, n, known, again)
    )


# --- detalj ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int) -> Response:
    state = _state(request)
    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise _not_found()
        sources = list(
            session.exec(select(JobSource).where(JobSource.job_id == job_id).order_by(JobSource.id))
        )
    return _render(request, "job_detail.html", {"job": job, "sources": sources})


# --- åtgärder -------------------------------------------------------------------------


def _row_view(state: AppState, job_id: int, error: str | None) -> RowView:
    with Session(state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise _not_found()
        return RowView(job=job, sources=_source_names(session, [job_id])[job_id], error=error)


def _respond(request: Request, job_id: int, error: str | None = None, flash: str = "") -> Response:
    """HTMX får den uppdaterade raden; vanliga formulär skickas tillbaka till listan."""
    if _is_htmx(request):
        row = _row_view(_state(request), job_id, error)
        return _render(request, "_job_row.html", {"row": row})
    return RedirectResponse(f"/?msg={flash}" if flash else "/", status_code=303)


def _act(request: Request, job_id: int, action) -> Response:
    try:
        action(job_id)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _respond(request, job_id, str(exc), "handled")
    return _respond(request, job_id)


@router.post("/jobs/{job_id}/link")
def mark_link(request: Request, job_id: int) -> Response:
    return _act(request, job_id, _state(request).service.mark_link)


@router.post("/jobs/{job_id}/dismiss")
def dismiss(request: Request, job_id: int) -> Response:
    return _act(request, job_id, _state(request).service.dismiss)


@router.post("/jobs/{job_id}/apply")
def apply(request: Request, job_id: int) -> Response:
    """Skriver brev och CV (ansökningspaketet) för alla ansökningssätt."""
    state = _state(request)
    try:
        state.service.start_draft(job_id)
    except LookupError:
        raise _not_found() from None
    except AlreadyHandled as exc:
        return _respond(request, job_id, str(exc), "handled")
    except (AIError, CVMissing, CVUnreadable) as exc:
        return _respond(request, job_id, f"Kunde inte skriva brevet: {exc}", "no_letter")
    target = f"/jobs/{job_id}/draft"
    if _is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


# --- Sök nu ---------------------------------------------------------------------------


@router.post("/run")
def run_now(request: Request) -> Response:
    try:
        _state(request).run_now()
    except RunInProgress:
        return RedirectResponse("/?msg=running", status_code=303)
    return RedirectResponse("/?msg=started", status_code=303)


# --- Importera LinkedIn-länkar ----------------------------------------------------------


@router.post("/import/linkedin")
def import_linkedin(request: Request, links: str = Form("")) -> Response:
    def again(error: str, status_code: int, rejected: list[str] | None = None) -> Response:
        form = ImportForm(text=links, error=error, rejected=rejected or [])
        return _render_list(request, import_form=form, status_code=status_code)

    try:
        ids, rejected = parse_linkedin_links(links)
    except TooManyLinks as exc:
        return again(str(exc), 400)
    if not ids:
        return again(NO_LINKS, 400, rejected)
    state = _state(request)
    known = _known_linkedin_jobs(state, ids)
    # Kända jobb hämtas bara igen om sidan inte gick att läsa förra gången.
    fetch = [i for i in ids if i not in known or known[i]]
    if fetch:
        try:
            state.import_links(fetch)
        except RunInProgress:
            return again(IN_PROGRESS, 409, rejected)
    added, refetched = len(ids) - len(known), sum(known.values())
    # Importerade jobb visas oavsett poäng.
    return RedirectResponse(
        f"/?source={LinkedInLinksSource.name}&more=1&msg=imported"
        f"&n={added}&known={len(known)}&again={refetched}",
        status_code=303,
    )


def _known_linkedin_jobs(state: AppState, ids: list[str]) -> dict[str, bool]:
    """Jobb-id:n bland `ids` som redan importerats, och om jobbet är ofullständigt."""
    with Session(state.engine) as session:
        rows = session.exec(
            select(JobSource.external_id, Job.incomplete)
            .join(Job, col(Job.id) == JobSource.job_id)
            .where(
                JobSource.source == LinkedInLinksSource.name,
                col(JobSource.external_id).in_(ids),
            )
        )
        return dict(rows.all())
