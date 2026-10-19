"""Kommandoraden `jobaplier`: serve, gmail-login, run-once och install-launchd."""

from __future__ import annotations

import argparse
import errno
import logging
import os
import shutil
import socket
import sys
from pathlib import Path
from string import Template
from xml.sax.saxutils import escape

# WeasyPrint hittar inte alltid Homebrews pango på macOS utan denna sökväg
# (sätts innan något som importerar WeasyPrint laddas).
if os.path.isdir("/opt/homebrew/lib"):
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib")

from jobaplier.config import (  # noqa: E402
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    AIConfig,
    get_paths,
    load_config,
)

HOST = DASHBOARD_HOST
PORT = DASHBOARD_PORT
PROJECT_DIR = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_DIR / "launchd" / "se.jobaplier.plist.template"
LAUNCHD_LABEL = "se.jobaplier"
DEFAULT_PATH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
BOOTSTRAP_COMMAND = (
    f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
)

log = logging.getLogger("jobaplier")


class CLIError(Exception):
    """Fel som visas för användaren (svenska) och ger exit-kod 1."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobaplier",
        description="Hittar jobb som matchar ditt CV och ansöker efter godkännande.",
    )
    commands = parser.add_subparsers(dest="command", metavar="KOMMANDO")
    commands.add_parser(
        "serve", help=f"starta dashboarden och schemat på http://{HOST}:{PORT} (standard)"
    )
    commands.add_parser("gmail-login", help="logga in mot Gmail (öppnar webbläsaren)")
    commands.add_parser("run-once", help="gör en sökkörning nu och skriv ut statistiken")
    commands.add_parser(
        "install-launchd", help="skriv launchd-agenten som startar tjänsten vid inloggning"
    )
    return parser


def command_of(args: argparse.Namespace) -> str:
    return args.command or "serve"


# --- serve -----------------------------------------------------------------------------


def _bind_socket() -> socket.socket:
    """Binder porten innan något annat görs: en andra instans ska avbrytas innan den
    rör databasen (startsvepet skulle annars kunna störa ett pågående utskick)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            raise CLIError(f"Port {PORT} används redan – körs JobAplier redan?") from exc
        raise
    return sock


def serve() -> None:
    sock = _bind_socket()

    import uvicorn

    from jobaplier.main import create_production_app

    app = create_production_app()
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT))
    server.run(sockets=[sock])


# --- gmail-login -------------------------------------------------------------------------


def gmail_login() -> None:
    from jobaplier.gmail import run_oauth_flow

    paths = get_paths()
    if not paths.gmail_credentials.is_file():
        raise CLIError(
            f"{paths.gmail_credentials} saknas. Skapa ett OAuth-klient-id (typ Desktop) i Google "
            "Cloud Console och spara JSON-filen där (se README)."
        )
    run_oauth_flow(paths.gmail_credentials, paths.gmail_token)
    print(f"Gmail-inloggningen är klar. Token sparad i {paths.gmail_token}.")


# --- run-once ------------------------------------------------------------------------------


def format_run(run) -> str:
    stats = run.stats_json or {}
    lines = [f"Körning {run.id} ({run.trigger})"]
    if run.finished_at is not None:
        seconds = (run.finished_at - run.started_at).total_seconds()
        lines[0] += f" klar efter {seconds:.0f} s"
    sources = stats.get("sources") or {}
    if sources:
        lines.append("Källor:")
        for name, info in sources.items():
            result = f"{info.get('count', 0)} annonser" if info.get("ok") else "fel"
            lines.append(f"  {name}: {result}")
    if stats:
        lines.append(
            f"Nya jobb: {stats.get('new', 0)}, bortfiltrerade: {stats.get('filtered', 0)}, "
            f"poängsatta: {stats.get('scored', 0)}, "
            f"poängsättning misslyckades: {stats.get('scoring_failed', 0)}"
        )
    lines.append(f"AI-kostnad: {run.ai_cost_usd:.4f} USD")
    if run.errors_json:
        lines.append("Fel:")
        lines.extend(f"  {name}: {message}" for name, message in run.errors_json.items())
    return "\n".join(lines)


def run_once() -> int:
    from jobaplier.main import build_runtime

    # Inget startsvep: tjänsten kan vara igång och mitt i ett utskick.
    runtime = build_runtime(get_paths(), startup_sweep=False)
    try:
        run = runtime.runner.run_sync("manual")
    finally:
        runtime.close()
        runtime.state.engine.dispose()
    if run is None:
        return 1
    print(format_run(run))
    return 1 if run.errors_json else 0


# --- install-launchd ---------------------------------------------------------------------


def launchd_path(claude_bin: str) -> str:
    """PATH för agenten: katalogen där Claude Code hittas först, sedan standardkatalogerna."""
    dirs: list[str] = []
    found = shutil.which(claude_bin)
    if found:
        dirs.append(str(Path(found).parent))
    dirs.extend(d for d in DEFAULT_PATH_DIRS if d not in dirs)
    return ":".join(dirs)


def render_plist(*, uv: str, project_dir: Path, path_env: str, home: str) -> str:
    values = {
        "UV": uv,
        "PROJECT_DIR": str(Path(project_dir).resolve()),
        "PATH": path_env,
        "HOME": home,
    }
    template = Template(TEMPLATE.read_text(encoding="utf-8"))
    return template.substitute({key: escape(value) for key, value in values.items()})


def _configured_claude_bin() -> str:
    try:
        return load_config(get_paths().config).ai.claude_bin
    except Exception:  # saknade eller ogiltiga inställningar: standardvärdet
        return AIConfig().claude_bin


def install_launchd(
    home: Path | None = None, project_dir: Path = PROJECT_DIR, uv: str | None = None
) -> Path:
    home = Path(home) if home is not None else Path.home()
    uv = uv or shutil.which("uv") or os.environ.get("UV")
    if not uv:
        raise CLIError("uv hittades inte i PATH – installera uv först (se README)")
    project_dir = Path(project_dir).resolve()
    # launchd skapar inte loggkatalogen själv.
    (project_dir / "data" / "logs").mkdir(parents=True, exist_ok=True)
    text = render_plist(
        uv=str(Path(uv).absolute()),
        project_dir=project_dir,
        path_env=launchd_path(_configured_claude_bin()),
        home=str(home),
    )
    target = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"Skrev {target}")
    print("Starta agenten (och vid varje inloggning framöver) med:")
    print(f"  {BOOTSTRAP_COMMAND}")
    return target


# --- main --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("weasyprint", "fontTools", "apscheduler.scheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    command = command_of(args)
    try:
        if command == "serve":
            serve()
        elif command == "gmail-login":
            gmail_login()
        elif command == "run-once":
            return run_once()
        elif command == "install-launchd":
            install_launchd()
    except CLIError as exc:
        print(f"Fel: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
