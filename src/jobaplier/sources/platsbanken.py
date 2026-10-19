"""Platsbanken via Arbetsförmedlingens öppna JobSearch-API (JobTech)."""

from datetime import datetime, timezone

import httpx

from jobaplier.config import Preferences
from jobaplier.models import ApplyMethod
from jobaplier.sources.ats import detect_ats
from jobaplier.sources.base import ApplyInfo, RawJob
from jobaplier.sources.http import get_with_retry

SEARCH_URL = "https://jobsearch.api.jobtechdev.se/search"
PAGE_SIZE = 100
MAX_OFFSET = 2000  # API:ets gräns; vi frågar aldrig med högre offset.


class PlatsbankenSource:
    name = "platsbanken"

    def __init__(self, prefs: Preferences, http: httpx.Client) -> None:
        self.prefs = prefs
        self.http = http

    def fetch(self, since: datetime) -> list[RawJob]:
        queries = self.prefs.roles or self.prefs.keywords
        jobs: dict[str, RawJob] = {}
        for query in queries:
            for hit in self._search(query, since):
                job = self._to_raw_job(hit)
                jobs.setdefault(job.external_id, job)
        return list(jobs.values())

    def _search(self, query: str, since: datetime):
        offset = 0
        while offset <= MAX_OFFSET:
            response = get_with_retry(
                self.http,
                SEARCH_URL,
                params={
                    "q": query,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "published-after": since.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
            data = response.json()
            hits = data.get("hits") or []
            yield from hits
            offset += PAGE_SIZE
            total = (data.get("total") or {}).get("value", 0)
            if not hits or offset >= total:
                return

    def _to_raw_job(self, hit: dict) -> RawJob:
        address = hit.get("workplace_address") or {}
        details = hit.get("application_details") or {}
        webpage_url = hit.get("webpage_url") or ""
        return RawJob(
            source=self.name,
            external_id=str(hit["id"]),
            title=hit.get("headline") or "",
            company=(hit.get("employer") or {}).get("name") or "",
            location=address.get("municipality") or address.get("region") or None,
            remote=None,
            description=(hit.get("description") or {}).get("text") or "",
            url=webpage_url,
            published_at=_parse_date(hit.get("publication_date")),
            apply=_apply_info(details),
        )


def _apply_info(details: dict) -> ApplyInfo:
    email = (details.get("email") or "").strip() or None
    apply_url = (details.get("url") or "").strip() or None
    reference = (details.get("reference") or "").strip() or None
    if email:
        return ApplyInfo(method=ApplyMethod.email, email=email, reference=reference)
    if apply_url and detect_ats(apply_url):
        return ApplyInfo(method=ApplyMethod.form, form_url=apply_url, reference=reference)
    return ApplyInfo(method=ApplyMethod.unknown, reference=reference)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
