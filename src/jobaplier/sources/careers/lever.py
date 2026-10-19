"""Lever-karriärsida via det publika Postings API:t."""

from datetime import datetime, timezone

import httpx

from jobaplier.config import CareerSite
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, RawJob, SourceError
from jobaplier.sources.careers import html_to_text, is_http_url
from jobaplier.sources.http import get_with_retry

POSTINGS_URL = "https://api.lever.co/v0/postings/{id}?mode=json"


class LeverSource:
    def __init__(self, site: CareerSite, http: httpx.Client) -> None:
        self.site = site
        self.http = http
        self.name = f"careers:lever:{site.id}"

    def fetch(self, since: datetime) -> list[RawJob]:
        url = POSTINGS_URL.format(id=self.site.id)
        try:
            data = get_with_retry(self.http, url).json()
        except ValueError as exc:
            raise SourceError(f"Svaret från {url} var inte giltig JSON.") from exc
        if not isinstance(data, list):
            raise SourceError(f"Svaret från {url} hade ett oväntat format.")
        jobs = []
        for entry in data:
            job = self._to_raw_job(entry) if isinstance(entry, dict) else None
            if job is None:
                continue
            if job.published_at is not None and job.published_at < since:
                continue
            jobs.append(job)
        return jobs

    def _to_raw_job(self, entry: dict) -> RawJob | None:
        hosted_url = entry.get("hostedUrl") or ""
        apply_url = entry.get("applyUrl") or hosted_url
        if not entry.get("id") or not is_http_url(hosted_url) or not is_http_url(apply_url):
            return None
        return RawJob(
            source=self.name,
            external_id=str(entry["id"]),
            title=entry.get("text") or "",
            company=self.site.company,
            location=(entry.get("categories") or {}).get("location") or None,
            remote=_remote(entry.get("workplaceType")),
            description=_description(entry),
            url=hosted_url,
            published_at=_parse_millis(entry.get("createdAt")),
            apply=ApplyInfo(method=ApplyMethod.form, form_url=apply_url),
        )


def _remote(workplace_type: str | None) -> bool | None:
    if not workplace_type:
        return None
    return workplace_type == "remote"


def _description(entry: dict) -> str:
    parts = [entry.get("descriptionPlain") or html_to_text(entry.get("description") or "")]
    for section in entry.get("lists") or []:
        heading = section.get("text") or ""
        body = html_to_text(section.get("content") or "")
        parts.append("\n".join(p for p in (heading, body) if p))
    parts.append(entry.get("additionalPlain") or html_to_text(entry.get("additional") or ""))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _parse_millis(value: int | None) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None
