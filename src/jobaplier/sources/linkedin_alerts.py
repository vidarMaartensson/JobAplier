"""LinkedIn-jobbaviseringar via Gmail, med publika jobbsidor som komplement."""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from selectolax.lexbor import LexborHTMLParser

from jobaplier.gmail import Gmail, GmailAuthError, GmailError
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, RawJob, SourceError
from jobaplier.sources.careers import html_to_text
from jobaplier.sources.http import get_with_retry

SENDER = "jobalerts-noreply@linkedin.com"
JOB_URL = "https://www.linkedin.com/jobs/view/{id}"
PAGE_DELAY_SECONDS = 1.0
AUTH_MESSAGE = "Gmail-inloggningen har gått ut – kör `jobaplier gmail-login`"

_JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_SKIPPED_TEXT_PARENTS = ("script", "style", "title")


def extract_job_refs(html: str) -> list[tuple[str, str, str]]:
    """Jobb-id, titel och företag ur ett aviseringsmejl, unika per id i
    dokumentordning.

    Titeln är länktextens text. Företaget är första icke-tomma textnod efter
    länken som inte ligger inuti någon jobblänk ("Visa jobb" räknas alltså
    inte). Hittas ingen innan nästa jobb börjar blir företaget "". Samma jobb
    kan förekomma flera gånger (logotyplänk, titellänk, "Visa jobb"); ett
    senare tillfälle fyller bara i det som saknas.
    """
    refs: list[list[str]] = []  # [id, titel, företag]
    index: dict[str, int] = {}
    pending: int | None = None  # jobb som väntar på sitt företag
    for node in LexborHTMLParser(html).root.traverse(include_text=True):
        if node.tag == "a":
            match = _JOB_LINK.search(node.attributes.get("href") or "")
            if match is None:
                continue
            job_id = match.group(1)
            if job_id not in index:
                index[job_id] = len(refs)
                refs.append([job_id, "", ""])
            ref = refs[index[job_id]]
            if not ref[1]:
                ref[1] = _clean(node.text(deep=True))
            pending = index[job_id] if not ref[2] else None
        elif node.tag == "-text" and pending is not None and _is_content_text(node):
            text = _clean(node.text(deep=False))
            if text:
                refs[pending][2] = text
                pending = None
    return [(job_id, title, company) for job_id, title, company in refs]


def _is_content_text(node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in _SKIPPED_TEXT_PARENTS:
            return False
        if parent.tag == "a" and _JOB_LINK.search(parent.attributes.get("href") or ""):
            return False
        parent = parent.parent
    return True


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


class LinkedInAlertsSource:
    name = "linkedin_alerts"

    def __init__(
        self,
        gmail: Gmail,
        http: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gmail = gmail
        self.http = http
        self.sleep = sleep

    def fetch(self, since: datetime) -> list[RawJob]:
        mails = self._search(since)
        refs: dict[str, tuple[str, str, datetime]] = {}
        for mail in mails:
            for job_id, title, company in extract_job_refs(mail.html):
                refs.setdefault(job_id, (title, company, mail.received_at))
        jobs = []
        for position, (job_id, (title, company, received_at)) in enumerate(refs.items()):
            if position:
                self.sleep(PAGE_DELAY_SECONDS)
            jobs.append(self._to_raw_job(job_id, title, company, received_at))
        return jobs

    def _search(self, since: datetime):
        # `since` är naiv UTC i projektet; naiv .timestamp() skulle tolkas som lokal tid.
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        query = f"from:{SENDER} after:{int(since.timestamp())}"
        try:
            return self.gmail.search(query)
        except GmailAuthError as exc:
            raise SourceError(AUTH_MESSAGE) from exc
        except GmailError as exc:
            raise SourceError(f"Kunde inte läsa LinkedIn-aviseringar från Gmail: {exc}") from exc

    def _to_raw_job(
        self, job_id: str, mail_title: str, mail_company: str, received_at: datetime
    ) -> RawJob:
        url = JOB_URL.format(id=job_id)
        page = self._fetch_page(url)
        return RawJob(
            source=self.name,
            external_id=job_id,
            title=page["title"] or mail_title,
            company=page["company"] or mail_company,
            location=page["location"] or None,
            remote=None,
            description=page["description"],
            url=url,
            published_at=received_at,
            apply=ApplyInfo(method=ApplyMethod.unknown),
            incomplete=not page["title"],
        )

    def _fetch_page(self, url: str) -> dict[str, str]:
        """Publik jobbsida. LinkedIn svarar ofta bots med 999; då (eller vid
        annat fel eller om titeln saknas) returneras tomma fält."""
        empty = {"title": "", "company": "", "location": "", "description": ""}
        try:
            # Ett enda försök utan backoff: en blockerad sida ska inte hamras.
            response = get_with_retry(self.http, url, attempts=1)
        except SourceError:
            return empty
        if response.status_code != 200:
            return empty
        tree = LexborHTMLParser(response.text)
        title = _text(tree, "h1.top-card-layout__title")
        if not title:
            return empty
        description = tree.css_first("div.show-more-less-html__markup")
        return {
            "title": title,
            "company": _text(tree, "a.topcard__org-name-link"),
            "location": _text(tree, "span.topcard__flavor--bullet"),
            "description": html_to_text(description.html) if description else "",
        }


def _text(tree: LexborHTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    return _clean(node.text(deep=True)) if node else ""
