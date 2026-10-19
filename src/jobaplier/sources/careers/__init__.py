"""Karriärsidor: Greenhouse, Lever och Teamtailor."""

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

from jobaplier.config import CareerSite
from jobaplier.sources.base import Source

# Blocktaggar får radbrytning före och efter (en blankrad mellan stycken);
# <br> och <li> bara efter, så att listor blir en post per rad.
_BLOCK_TAGS = ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr")
_LINE_TAGS = ("br", "li")


def html_to_text(raw: str) -> str:
    """HTML (även HTML-escapad, som Greenhouse levererar) till ren text.

    Blanksteg i raden slås ihop och flera tomma rader blir en.
    """
    if not raw:
        return ""
    tree = LexborHTMLParser(html.unescape(raw))
    for node in tree.css("script, style"):
        node.decompose()
    for node in tree.css(", ".join(_BLOCK_TAGS)):
        node.insert_before("\n")
        node.insert_after("\n")
    for node in tree.css(", ".join(_LINE_TAGS)):
        node.insert_after("\n")
    text = tree.text(deep=True, separator="")
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def to_naive_utc(value: datetime) -> datetime:
    """Aware tidsstämplar konverteras till UTC; alla returneras utan tzinfo."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def is_http_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        return urlsplit(url.strip()).scheme in ("http", "https")
    except ValueError:
        return False


def build_career_sources(sites: list[CareerSite], http: httpx.Client) -> list[Source]:
    # Importeras här eftersom källmodulerna importerar hjälpfunktionerna ovan.
    from jobaplier.sources.careers.greenhouse import GreenhouseSource
    from jobaplier.sources.careers.lever import LeverSource
    from jobaplier.sources.careers.teamtailor import TeamtailorSource

    classes = {
        "greenhouse": GreenhouseSource,
        "lever": LeverSource,
        "teamtailor": TeamtailorSource,
    }
    return [classes[site.type](site, http) for site in sites]
