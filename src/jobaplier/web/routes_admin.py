"""Ansökningshistorik, körningar, strukturerat CV, inställningar och installationssida."""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, col, select

from jobaplier.ai import monthly_spend
from jobaplier.config import CareerSite, Config, ProfileInfo, load_config, save_config
from jobaplier.models import AICall, Application, ApplicationStatus, Channel, Job, Run, utcnow
from jobaplier.web.routes_jobs import _render, _state

router = APIRouter()

RUN_LIMIT = 50
CHANNEL_LABELS = {Channel.email: "Mejl", Channel.form: "Formulär", Channel.manual: "Länk (manuell)"}
APPLICATION_STATUS_LABELS = {
    ApplicationStatus.submitting: "pågår",
    ApplicationStatus.submitted: "skickad",
    ApplicationStatus.failed: "misslyckad",
}
TRIGGER_LABELS = {"schedule": "Schema", "manual": "Manuell"}
SETTINGS_FLASH = {"saved": "Sparat"}
CAREER_TYPES: tuple[str, ...] = get_args(CareerSite.model_fields["type"].annotation)


# --- ansökningar ----------------------------------------------------------------------------


@router.get("/applications")
def applications(request: Request) -> Response:
    state = _state(request)
    with Session(state.engine) as session:
        rows = list(
            session.exec(
                select(Application, Job)
                .join(Job, col(Job.id) == col(Application.job_id))
                .order_by(
                    func.coalesce(col(Application.submitted_at), datetime.max).desc(),
                    col(Application.id).desc(),
                )
            )
        )
    return _render(
        request,
        "applications.html",
        {
            "rows": rows,
            "channel_labels": CHANNEL_LABELS,
            "app_status_labels": APPLICATION_STATUS_LABELS,
        },
    )


# --- körningar ------------------------------------------------------------------------------


@dataclass
class Usage:
    calls: int
    input_tokens: int
    output_tokens: int


def _month_usage(state) -> Usage:
    now = utcnow()
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    with Session(state.engine) as session:
        calls, tokens_in, tokens_out = session.exec(
            select(
                func.count(),
                func.coalesce(func.sum(AICall.input_tokens), 0),
                func.coalesce(func.sum(AICall.output_tokens), 0),
            ).where(AICall.at >= start, AICall.at < end)
        ).one()
    return Usage(calls=calls, input_tokens=tokens_in, output_tokens=tokens_out)


@router.get("/runs")
def runs(request: Request) -> Response:
    state = _state(request)
    config = state.config()
    with Session(state.engine) as session:
        run_rows = list(
            session.exec(
                select(Run).order_by(col(Run.started_at).desc(), col(Run.id).desc()).limit(RUN_LIMIT)
            )
        )
    context: dict[str, Any] = {
        "runs": run_rows,
        "trigger_labels": TRIGGER_LABELS,
        "backend": config.ai.backend,
    }
    if config.ai.backend == "api":
        context["spend"] = monthly_spend(state.engine)
        context["budget"] = config.ai_monthly_budget_usd
    else:
        context["usage"] = _month_usage(state)
    return _render(request, "runs.html", context)


# --- strukturerat CV ------------------------------------------------------------------------


@router.get("/cv")
def cv_page(request: Request) -> Response:
    """Så har CV:t tolkats. Fakta i de skräddarsydda CV:na kopieras härifrån."""
    state = _state(request)
    cv = state.cv_document()
    warnings = state.cv_warnings() if cv is not None else []
    return _render(request, "cv.html", {"cv": cv, "warnings": warnings})


# --- inställningar --------------------------------------------------------------------------

# Formulärfältens namn och vilken väg i Config de hör till.
TEXT_FIELDS = {
    "profile_name": ("profile", "name"),
    "profile_email": ("profile", "email"),
    "profile_phone": ("profile", "phone"),
    "profile_linkedin_url": ("profile", "linkedin_url"),
    "threshold": ("scoring", "threshold"),
    "ai_backend": ("ai", "backend"),
    "claude_bin": ("ai", "claude_bin"),
    "ai_monthly_budget_usd": ("ai_monthly_budget_usd",),
}
LIST_FIELDS = {
    "roles": ("preferences", "roles"),
    "keywords": ("preferences", "keywords"),
    "exclude_keywords": ("preferences", "exclude_keywords"),
    "exclude_companies": ("preferences", "exclude_companies"),
    "locations": ("preferences", "locations"),
    "seniority": ("preferences", "seniority"),
    "schedule": ("schedule",),
}
CHECKBOXES = {
    "remote_ok": ("preferences", "remote_ok"),
    "platsbanken": ("sources", "platsbanken", "enabled"),
}
# Pydantic-felens `loc` (de första elementen) → formulärfält.
LOC_TO_FIELD = {
    **{path: name for name, path in TEXT_FIELDS.items()},
    **{path: name for name, path in LIST_FIELDS.items()},
    **{path: name for name, path in CHECKBOXES.items()},
    ("sources", "careers"): "careers",
}
ERROR_MESSAGES = {
    "missing": "Obligatoriskt",
    "int_parsing": "Måste vara ett heltal",
    "int_from_float": "Måste vara ett heltal",
    "float_parsing": "Måste vara ett tal (använd punkt som decimaltecken)",
    "literal_error": "Ogiltigt val",
}


def _blank_config() -> Config:
    """Standardvärden när config.yaml saknas; namn och e-post fylls i av användaren."""
    return Config(profile=ProfileInfo(name="", email=""))


def _current_config(state) -> Config:
    try:
        return state.config()
    except (OSError, ValueError, yaml.YAMLError):  # config.yaml saknas eller går inte att läsa
        return _blank_config()


def _describe_load_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(map(str, e['loc']))}: {_error_message(e)}" for e in exc.errors()[:3]
        ]
        return "; ".join(parts)
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _config_file_problem(path) -> str | None:
    """Svensk förklaring om config.yaml finns men inte går att läsa eller är ogiltig."""
    if not path.is_file():
        return None
    try:
        load_config(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return f"config.yaml kunde inte läsas: {_describe_load_error(exc)}"
    return None


def _dig(data: dict, path: tuple[str, ...]) -> Any:
    for key in path:
        data = data[key]
    return data


def _put(data: dict, path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


def _join(values: list[str]) -> str:
    return "\n".join(values)


def _form_values(config: Config) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    form: dict[str, Any] = {name: str(_dig(data, path)) for name, path in TEXT_FIELDS.items()}
    form.update({name: _join(_dig(data, path)) for name, path in LIST_FIELDS.items()})
    form.update({name: bool(_dig(data, path)) for name, path in CHECKBOXES.items()})
    form["careers"] = "\n".join(
        f"{c['type']},{c['company']},{c['id']}" for c in data["sources"]["careers"]
    )
    return form


def _split_list(value: str) -> list[str]:
    """Ett värde per rad (värden får innehålla kommatecken)."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _parse_careers(text: str, errors: dict[str, str]) -> list[dict[str, str]]:
    """En sajt per rad: `typ,företag,id`. Typ är första fältet och id det sista; företaget är
    allt däremellan (får alltså innehålla kommatecken). Tomma rader hoppas över."""
    sites: list[dict[str, str]] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(",")
        kind, company, site_id = (
            parts[0].strip(),
            ",".join(parts[1:-1]).strip(),
            parts[-1].strip(),
        )
        if len(parts) < 3 or not (kind and company and site_id):
            problems.append(f"Rad {number}: förväntat formatet typ,företag,id")
        elif kind not in CAREER_TYPES:
            problems.append(f"Rad {number}: okänd typ ”{kind}” (välj {', '.join(CAREER_TYPES)})")
        else:
            sites.append({"type": kind, "company": company, "id": site_id})
    if problems:
        errors["careers"] = "; ".join(problems)
    return sites


def _error_message(error: Any) -> str:
    kind = error["type"]
    if kind in ERROR_MESSAGES:
        return ERROR_MESSAGES[kind]
    if kind == "value_error":
        return str(error["msg"]).removeprefix("Value error, ")
    return "Ogiltigt värde"


def _field_for(loc: tuple) -> str | None:
    for size in (3, 2, 1):
        field = LOC_TO_FIELD.get(tuple(loc[:size]))
        if field is not None and len(loc[:size]) == size:
            return field
    return None


def _validate_extra(form: dict[str, Any], errors: dict[str, str]) -> None:
    if not form["profile_name"].strip():
        errors["profile_name"] = "Obligatoriskt"
    email = form["profile_email"].strip()
    if not email:
        errors["profile_email"] = "Obligatoriskt"
    elif "@" not in email or " " in email:
        errors["profile_email"] = "Ogiltig e-postadress"
    try:
        if not 0 <= int(form["threshold"]) <= 100:
            errors["threshold"] = "Måste vara ett heltal mellan 0 och 100"
    except ValueError:
        pass  # Pydantic rapporterar att det inte är ett heltal
    try:
        budget = float(form["ai_monthly_budget_usd"])
        if not math.isfinite(budget):
            errors["ai_monthly_budget_usd"] = "Måste vara ett ändligt tal"
        elif budget < 0:
            errors["ai_monthly_budget_usd"] = "Får inte vara negativt"
    except ValueError:
        pass


def _settings_page(
    request: Request,
    form: dict[str, Any],
    errors: dict[str, str] | None = None,
    flash: str = "",
    status_code: int = 200,
) -> Response:
    return _render(
        request,
        "settings.html",
        {
            "form": form,
            "errors": errors or {},
            "flash": SETTINGS_FLASH.get(flash),
            "career_types": CAREER_TYPES,
            "file_problem": _config_file_problem(_state(request).paths.config),
        },
        status_code=status_code,
    )


@router.get("/settings")
def settings(request: Request, msg: str = "") -> Response:
    return _settings_page(request, _form_values(_current_config(_state(request))), flash=msg)


@router.post("/settings")
async def save_settings(request: Request) -> Response:
    state = _state(request)
    posted = await request.form()
    base = _current_config(state)
    data = base.model_dump(mode="json")
    form = _form_values(base)
    errors: dict[str, str] = {}

    # Fält som formuläret inte skickar behåller sina nuvarande värden; en ikryssad ruta
    # som saknas betyder "av".
    for name in (*TEXT_FIELDS, *LIST_FIELDS, "careers"):
        if name in posted:
            form[name] = str(posted[name])
    for name in CHECKBOXES:
        form[name] = name in posted

    for name, path in TEXT_FIELDS.items():
        _put(data, path, form[name].strip())
    for name, path in LIST_FIELDS.items():
        _put(data, path, _split_list(form[name]))
    for name, path in CHECKBOXES.items():
        _put(data, path, form[name])
    data["sources"]["careers"] = _parse_careers(form["careers"], errors)
    _validate_extra(form, errors)

    config = None
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            field = _field_for(error["loc"])
            message = _error_message(error)
            if field is None:
                errors.setdefault("_general", f"{'.'.join(map(str, error['loc']))}: {message}")
            else:
                errors.setdefault(field, message)

    if errors or config is None:
        return _settings_page(request, form, errors, status_code=400)
    if _config_file_problem(state.paths.config) is not None:
        # Skriv inte över en trasig fil utan att spara undan den först.
        shutil.copy2(state.paths.config, state.paths.config.with_name("config.yaml.bak"))
    save_config(config, state.paths.config)
    state.on_config_saved(config)
    return RedirectResponse("/settings?msg=saved", status_code=303)


# --- installationssida ----------------------------------------------------------------------


@router.get("/setup")
def setup(request: Request) -> Response:
    return _render(request, "setup.html", {"missing": _state(request).setup_missing()})
