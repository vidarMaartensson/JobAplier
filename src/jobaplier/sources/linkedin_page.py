"""Publik LinkedIn-jobbsida: hämtning och tolkning."""

import re
from dataclasses import dataclass

import httpx
from selectolax.lexbor import LexborHTMLParser

from jobaplier.sources.base import SourceError
from jobaplier.sources.careers import html_to_text
from jobaplier.sources.http import get_with_retry

JOB_URL = "https://www.linkedin.com/jobs/view/{id}"


@dataclass
class JobPage:
    title: str
    company: str
    location: str | None
    description: str


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_job_page(html: str) -> JobPage | None:
    """Tolkar en publik jobbsida. `None` om titeln saknas (t.ex. en inloggningsvägg)."""
    tree = LexborHTMLParser(html)
    title = _text(tree, "h1.top-card-layout__title")
    if not title:
        return None
    description = tree.css_first("div.show-more-less-html__markup")
    return JobPage(
        title=title,
        company=_text(tree, "a.topcard__org-name-link"),
        location=_text(tree, "span.topcard__flavor--bullet") or None,
        description=html_to_text(description.html) if description else "",
    )


def fetch_job_page(http: httpx.Client, job_id: str) -> JobPage | None:
    """Hämtar jobbsidan. LinkedIn svarar ofta bots med 999; då (eller vid annat
    fel eller om sidan saknar titel) blir resultatet `None`."""
    try:
        # Ett enda försök utan backoff: en blockerad sida ska inte hamras.
        response = get_with_retry(http, JOB_URL.format(id=job_id), attempts=1)
    except SourceError:
        return None
    if response.status_code != 200:
        return None
    return parse_job_page(response.text)


def _text(tree: LexborHTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    return clean_text(node.text(deep=True)) if node else ""
