"""Teamtailor-karriärsida via sidans RSS-flöde."""

import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from jobaplier.config import CareerSite
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, RawJob, SourceError
from jobaplier.sources.careers import html_to_text, is_http_url, to_naive_utc
from jobaplier.sources.http import get_with_retry

FEED_URL = "https://{id}.teamtailor.com/jobs.rss"
_TT = "{https://teamtailor.com/locations}"


class TeamtailorSource:
    def __init__(self, site: CareerSite, http: httpx.Client) -> None:
        self.site = site
        self.http = http
        self.name = f"careers:teamtailor:{site.id}"

    def _feed_url(self) -> str:
        # Ett id med punkt är en egen domän, t.ex. "karriar.exempel.se".
        if "." in self.site.id:
            return f"https://{self.site.id}/jobs.rss"
        return FEED_URL.format(id=self.site.id)

    def fetch(self, since: datetime) -> list[RawJob]:
        url = self._feed_url()
        response = get_with_retry(self.http, url)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SourceError(f"Svaret från {url} var inte giltig XML.") from exc
        jobs = []
        for item in root.iterfind("./channel/item"):
            job = self._to_raw_job(item)
            if job is None:
                continue
            if job.published_at is not None and job.published_at < since:
                continue
            jobs.append(job)
        return jobs

    def _to_raw_job(self, item: ET.Element) -> RawJob | None:
        link = (item.findtext("link") or "").strip()
        if not is_http_url(link):
            return None
        return RawJob(
            source=self.name,
            external_id=(item.findtext("guid") or link).strip(),
            title=(item.findtext("title") or "").strip(),
            company=self.site.company,
            location=_location(item),
            remote=_remote(item.findtext("remoteStatus")),
            description=html_to_text(item.findtext("description") or ""),
            url=link,
            published_at=_parse_pub_date(item.findtext("pubDate")),
            apply=ApplyInfo(method=ApplyMethod.form, form_url=link),
        )


def _location(item: ET.Element) -> str | None:
    location = item.find(f"{_TT}locations/{_TT}location")
    if location is None:
        return None
    place = location.findtext(f"{_TT}city") or location.findtext(f"{_TT}name") or ""
    return place.strip() or None


def _remote(status: str | None) -> bool | None:
    status = (status or "").strip().lower()
    if status in ("fully", "remote"):
        return True
    if status in ("none", "hybrid"):
        return False
    return None


def _parse_pub_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_naive_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError):
        return None
