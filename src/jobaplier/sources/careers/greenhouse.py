"""Greenhouse jobbtavla via det publika Job Board API:t."""

from datetime import datetime

import httpx

from jobaplier.config import CareerSite
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, RawJob, SourceError
from jobaplier.sources.careers import html_to_text, is_http_url, to_naive_utc
from jobaplier.sources.http import get_with_retry

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{id}/jobs?content=true"


class GreenhouseSource:
    def __init__(self, site: CareerSite, http: httpx.Client) -> None:
        self.site = site
        self.http = http
        self.name = f"careers:greenhouse:{site.id}"

    def fetch(self, since: datetime) -> list[RawJob]:
        url = BOARD_URL.format(id=self.site.id)
        try:
            data = get_with_retry(self.http, url).json()
        except ValueError as exc:
            raise SourceError(f"Svaret från {url} var inte giltig JSON.") from exc
        entries = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise SourceError(f"Svaret från {url} hade ett oväntat format.")
        jobs = []
        for entry in entries:
            job = self._to_raw_job(entry)
            if job is None:
                continue
            if job.published_at is not None and job.published_at < since:
                continue
            jobs.append(job)
        return jobs

    def _to_raw_job(self, entry: dict) -> RawJob | None:
        job_url = entry.get("absolute_url") or ""
        if "id" not in entry or not is_http_url(job_url):
            return None
        return RawJob(
            source=self.name,
            external_id=str(entry["id"]),
            title=entry.get("title") or "",
            company=self.site.company,
            location=(entry.get("location") or {}).get("name") or None,
            remote=None,
            description=html_to_text(entry.get("content") or ""),
            url=job_url,
            published_at=_parse_timestamp(entry.get("updated_at")),
            apply=ApplyInfo(method=ApplyMethod.form, form_url=job_url),
        )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
