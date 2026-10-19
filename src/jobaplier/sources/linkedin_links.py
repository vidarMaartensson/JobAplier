"""LinkedIn-jobb som användaren klistrat in som länkar."""

import re
import time
from collections.abc import Callable
from datetime import datetime

import httpx

from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, RawJob
from jobaplier.sources.linkedin_page import JOB_URL, fetch_job_page

MAX_LINKS = 20
PAUSE_SECONDS = 1.0
_LINK_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#\s]*-)?(\d+)", re.IGNORECASE)


class TooManyLinks(ValueError):
    """Fler än `MAX_LINKS` jobb-id:n i en och samma import."""

    def __init__(self) -> None:
        super().__init__(f"Högst {MAX_LINKS} länkar åt gången")


def parse_linkedin_links(text: str) -> tuple[list[str], list[str]]:
    """Returnerar de unika jobb-id:na i första förekomstens ordning och de icke-tomma
    rader som inte är LinkedIn-jobblänkar. Kastar `TooManyLinks` om id:na är för många."""
    ids: list[str] = []
    rejected: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LINK_RE.search(line)
        if match is None:
            rejected.append(line)
        elif match.group(1) not in ids:
            ids.append(match.group(1))
    if len(ids) > MAX_LINKS:
        raise TooManyLinks
    return ids, rejected


class LinkedInLinksSource:
    """Hämtar de angivna jobbsidorna. En sida som inte går att läsa ger ett ofullständigt
    jobb (titel, URL och id) i stället för ett fel."""

    name = "linkedin_links"
    skip_filter = True  # användaren har valt jobben själv

    def __init__(
        self,
        job_ids: list[str],
        http: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._job_ids = job_ids
        self._http = http
        self._sleep = sleep

    def fetch(self, since: datetime) -> list[RawJob]:
        jobs: list[RawJob] = []
        for index, job_id in enumerate(self._job_ids):
            if index:
                self._sleep(PAUSE_SECONDS)
            page = fetch_job_page(self._http, job_id)
            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=job_id,
                    title=page.title if page else f"LinkedIn-jobb {job_id}",
                    company=(page.company if page else "") or "Okänt",
                    location=page.location if page else None,
                    remote=None,
                    description=page.description if page else "",
                    url=JOB_URL.format(id=job_id),
                    published_at=None,
                    apply=ApplyInfo(ApplyMethod.unknown),
                    incomplete=page is None,
                )
            )
        return jobs
