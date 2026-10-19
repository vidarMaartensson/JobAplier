"""Kontroll av vad som saknas för att tjänsten ska fungera (visas på /setup)."""

from __future__ import annotations

from collections.abc import Callable

from jobaplier.ai import claude_code_available
from jobaplier.config import AIConfig, Config, Paths

CV_MISSING = "CV saknas – lägg ditt CV som data/cv.pdf"
CONFIG_MISSING = "Inställningar saknas – fyll i dem under Inställningar (sparas som data/config.yaml)"
CLAUDE_MISSING = "Claude Code hittas inte – installera och logga in med `claude`"
API_KEY_MISSING = "ANTHROPIC_API_KEY saknas – lägg nyckeln i .env"
GMAIL_MISSING = (
    "Gmail-inloggning saknas – lägg data/credentials.json på plats och kör `jobaplier gmail-login`"
)


def compute_setup_missing(
    paths: Paths,
    config_loader: Callable[[], Config | None],
    api_key: Callable[[], str | None],
) -> list[str]:
    """Svenska beskrivningar av det som saknas; tom lista betyder att allt är klart.

    `config_loader` returnerar `None` eller kastar om inställningarna saknas eller är
    ogiltiga. Då antas standardbackenden så att AI-kontrollen ändå går att göra."""
    missing: list[str] = []
    if not paths.cv.is_file():
        missing.append(CV_MISSING)

    try:
        config = config_loader()
    except Exception:  # saknad, oläsbar eller ogiltig config.yaml
        config = None
    if config is None:
        missing.append(CONFIG_MISSING)
    ai = config.ai if config is not None else AIConfig()

    if ai.backend == "api":
        if not api_key():
            missing.append(API_KEY_MISSING)
    elif not claude_code_available(ai.claude_bin):
        missing.append(CLAUDE_MISSING)

    if not paths.gmail_token.is_file():
        missing.append(GMAIL_MISSING)
    return missing
