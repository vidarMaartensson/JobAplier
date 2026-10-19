"""Igenkänning av rekryteringssystem (ATS) utifrån domännamn."""

from urllib.parse import urlsplit

# (system, domänsuffix). Matchar värdnamnet exakt eller som underdomän.
_ATS_DOMAINS: tuple[tuple[str, str], ...] = (
    ("teamtailor", "teamtailor.com"),
    ("varbi", "varbi.com"),
    ("reachmee", "reachmee.com"),
    ("greenhouse", "boards.greenhouse.io"),
    ("greenhouse", "job-boards.greenhouse.io"),
    ("lever", "jobs.lever.co"),
    ("workday", "myworkdayjobs.com"),
)


def detect_ats(url: str) -> str | None:
    """Returnerar ATS-namn för känd domän, annars None (även för trasiga URL:er)."""
    try:
        host = urlsplit(url.strip()).hostname
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    for name, domain in _ATS_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return name
    return None
