"""FastAPI-appen: mallar, statiska filer och routrar."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobaplier.config import DASHBOARD_PORT
from jobaplier.web.state import AppState

WEB_DIR = Path(__file__).parent
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FORBIDDEN = "Förbjudet: begäran kom inte från dashboarden på den här datorn."
LOCAL_TZ = ZoneInfo("Europe/Stockholm")
# Sökvägar som fungerar även när installationen är ofullständig.
SETUP_EXEMPT = ("/setup", "/settings")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def local_origins(port: int = DASHBOARD_PORT) -> frozenset[str]:
    """Dashboardens egna origins (den lyssnar bara på 127.0.0.1)."""
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def _origin(value: str | None) -> tuple[str, str, int] | None:
    """(schema, värd, port) för en Origin-/Referer-URL; saknad port blir standardporten."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return scheme, parts.hostname, port or _DEFAULT_PORTS[scheme]


def _host_port(value: str | None) -> tuple[str, int | None] | None:
    """Värdnamn och port (eller `None` om den saknas) i en Host-header."""
    if not value:
        return None
    try:
        parts = urlsplit(f"//{value}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname, port


def safe_url(value: str | None) -> str:
    """Bara http(s)-länkar släpps igenom. Annonsdata är opålitlig och en länk som
    `javascript:` får aldrig hamna i ett href-attribut (autoescape stoppar inte det)."""
    if not value:
        return ""
    value = value.strip()
    try:
        parts = urlsplit(value)
    except ValueError:  # t.ex. "http://[abc": en trasig annons får inte fälla sidan
        return ""
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return value
    return ""


def localtime(value: datetime | None) -> str:
    """Naiv UTC (så lagras alla tidsstämplar) som lokal tid, `ÅÅÅÅ-MM-DD TT:MM`."""
    if value is None:
        return "–"
    return value.replace(tzinfo=UTC).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _setup_exempt(path: str) -> bool:
    return path in SETUP_EXEMPT or path == "/static" or path.startswith("/static/")


def create_app(state: AppState, allowed_origins: Iterable[str] | None = None) -> FastAPI:
    """`allowed_origins`: exakta origins (schema, värd och port) som dashboarden får
    anropas från; standard är `local_origins()`."""
    from jobaplier.web import routes_admin, routes_drafts, routes_jobs

    app = FastAPI(title="JobAplier", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobaplier = state

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters["safe_url"] = safe_url
    templates.env.filters["localtime"] = localtime
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(routes_jobs.router)
    app.include_router(routes_drafts.router)
    app.include_router(routes_admin.router)

    if allowed_origins is None:
        allowed_origins = local_origins()
    origins = {o for o in map(_origin, allowed_origins) if o is not None}
    host_ports = {(host, port) for _scheme, host, port in origins}
    hostnames = {host for host, _port in host_ports}

    # Starlette lägger senare middleware yttre: den här (installationskontrollen) är
    # definierad före `_guard_origin` och körs därför efter Host-/CSRF-kontrollen.
    @app.middleware("http")
    async def _require_setup(request: Request, call_next):
        """Tills det som saknas är på plats leder allt utom /setup, /settings och
        /static till installationssidan (även POST, så att inget skickas halvfärdigt)."""
        if not _setup_exempt(request.url.path) and state.setup_missing():
            return RedirectResponse("/setup", status_code=303)
        return await call_next(request)

    @app.middleware("http")
    async def _guard_origin(request: Request, call_next):
        """Skydd mot DNS-rebinding (Host måste vara dashboardens värd och port) och CSRF
        från andra webbplatser (Origin, annars Referer, måste vara exakt dashboardens
        origin för alla skrivande anrop; en sida på en annan port på localhost är en
        annan webbplats)."""
        host = _host_port(request.headers.get("host"))
        ok = host is not None and (
            host in host_ports or (host[1] is None and host[0] in hostnames)
        )
        if ok and request.method not in SAFE_METHODS:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin is not None:
                ok = _origin(origin) in origins
        if not ok:
            return PlainTextResponse(FORBIDDEN, status_code=403)
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            message = exc.detail if exc.detail != "Not Found" else "Sidan finns inte"
            return templates.TemplateResponse(
                request, "not_found.html", {"message": message}, status_code=404
            )
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    return app
