from datetime import datetime
from pathlib import Path

import httpx
import pytest

from jobaplier.config import CareerSite
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import SourceError
from jobaplier.sources.careers import build_career_sources, html_to_text
from jobaplier.sources.careers.greenhouse import GreenhouseSource
from jobaplier.sources.careers.lever import LeverSource
from jobaplier.sources.careers.teamtailor import TeamtailorSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "careers"
SINCE = datetime(2026, 10, 5, 8, 30, 15)
EPOCH = datetime(1970, 1, 1)


def _client(body: bytes, requests: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _greenhouse(body: bytes | None = None, requests=None) -> GreenhouseSource:
    site = CareerSite(type="greenhouse", company="Exempel AB", id="exempel")
    return GreenhouseSource(site, _client(body or _fixture("greenhouse.json"), requests))


def _lever(body: bytes | None = None, requests=None) -> LeverSource:
    site = CareerSite(type="lever", company="Exempel AB", id="exempel")
    return LeverSource(site, _client(body or _fixture("lever.json"), requests))


def _teamtailor(body: bytes | None = None, requests=None, site_id="exempel") -> TeamtailorSource:
    site = CareerSite(type="teamtailor", company="Exempel AB", id=site_id)
    return TeamtailorSource(site, _client(body or _fixture("teamtailor.rss"), requests))


def test_greenhouse_maps_and_unescapes():
    requests: list[httpx.Request] = []
    source = _greenhouse(requests=requests)
    assert source.name == "careers:greenhouse:exempel"
    (job,) = source.fetch(SINCE)
    assert str(requests[0].url) == (
        "https://boards-api.greenhouse.io/v1/boards/exempel/jobs?content=true"
    )
    assert job.source == "careers:greenhouse:exempel"
    assert job.external_id == "6136160004"
    assert job.title == "Backend Engineer"
    assert job.company == "Exempel AB"
    assert job.location == "Stockholm"
    assert job.url == "https://job-boards.greenhouse.io/exempel/jobs/6136160004"
    assert job.published_at == datetime(2026, 10, 10, 7, 0, 0)
    assert "<" not in job.description and "&lt;" not in job.description
    assert "About us" in job.description
    assert "great software & services." in job.description
    assert "Python" in job.description
    assert job.apply.method == ApplyMethod.form
    assert job.apply.form_url == "https://job-boards.greenhouse.io/exempel/jobs/6136160004"


def test_lever_remote_and_apply_url():
    requests: list[httpx.Request] = []
    source = _lever(requests=requests)
    assert source.name == "careers:lever:exempel"
    (job,) = source.fetch(SINCE)
    assert str(requests[0].url) == "https://api.lever.co/v0/postings/exempel?mode=json"
    assert job.source == "careers:lever:exempel"
    assert job.external_id == "223f9722-3579-4ff2-b123-e4cc7b0c1a64"
    assert job.title == "Software Developer"
    assert job.company == "Exempel AB"
    assert job.location == "Stockholm"
    assert job.remote is True
    assert job.url == "https://jobs.lever.co/exempel/223f9722-3579-4ff2-b123-e4cc7b0c1a64"
    assert job.published_at == datetime(2026, 10, 10, 9, 0, 0)
    assert job.apply.method == ApplyMethod.form
    assert job.apply.form_url == (
        "https://jobs.lever.co/exempel/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply"
    )
    assert "platform team" in job.description
    assert "Build APIs" in job.description
    assert "<" not in job.description


def test_lever_onsite_is_not_remote():
    jobs = _lever().fetch(EPOCH)
    old = next(j for j in jobs if j.title == "Old Role")
    assert old.remote is False


def test_teamtailor_parses_items():
    requests: list[httpx.Request] = []
    source = _teamtailor(requests=requests)
    assert source.name == "careers:teamtailor:exempel"
    (job,) = source.fetch(SINCE)
    assert str(requests[0].url) == "https://exempel.teamtailor.com/jobs.rss"
    assert job.source == "careers:teamtailor:exempel"
    assert job.external_id == "4ff07fd5-3ac0-4333-8f4c-955380396321"
    assert job.title == "Systemutvecklare"
    assert job.company == "Exempel AB"
    assert job.location == "Stockholm"
    assert job.remote is True
    assert job.url == "https://exempel.teamtailor.com/jobs/8118064-systemutvecklare"
    assert job.published_at == datetime(2026, 10, 10, 7, 5, 16)
    assert "<" not in job.description and "&amp;" not in job.description
    assert "Vi söker en systemutvecklare & kollega." in job.description
    assert "Java" in job.description
    assert job.apply.method == ApplyMethod.form
    assert job.apply.form_url == job.url


def test_teamtailor_custom_domain_uses_id_as_host():
    requests: list[httpx.Request] = []
    _teamtailor(requests=requests, site_id="karriar.exempel.se").fetch(SINCE)
    assert str(requests[0].url) == "https://karriar.exempel.se/jobs.rss"


@pytest.mark.parametrize("make", [_greenhouse, _lever, _teamtailor])
def test_since_filters_old(make):
    source = make()
    assert len(source.fetch(SINCE)) == 1
    assert len(source.fetch(EPOCH)) == 2
    assert source.fetch(datetime(2027, 1, 1)) == []


def test_greenhouse_since_compares_in_utc():
    # 09:00+02:00 = 07:00 UTC, alltså före 08:30:15 UTC men efter om offseten ignorerades.
    ids = {j.external_id for j in _greenhouse().fetch(SINCE)}
    assert "6136160005" not in ids


def test_skips_jobs_with_non_http_apply_url():
    ids = {j.external_id for j in _greenhouse().fetch(EPOCH)}
    assert ids == {"6136160004", "6136160005"}


@pytest.mark.parametrize("make", [_greenhouse, _lever])
def test_invalid_json_raises_source_error(make):
    with pytest.raises(SourceError, match="JSON"):
        make(b"<html>not json</html>").fetch(SINCE)


@pytest.mark.parametrize("make", [_greenhouse, _lever])
def test_unexpected_json_shape_raises_source_error(make):
    with pytest.raises(SourceError):
        make(b'"nope"').fetch(SINCE)


def test_malformed_xml_raises_source_error():
    with pytest.raises(SourceError, match="XML"):
        _teamtailor(b"<rss><channel><item>").fetch(SINCE)


def test_html_to_text_unescapes_strips_and_collapses():
    text = html_to_text("&lt;p&gt;Hej&lt;/p&gt;\n\n\n&lt;p&gt;Då &amp;amp; tack&lt;/p&gt;<br>")
    assert text == "Hej\n\nDå & tack"
    assert html_to_text("") == ""
    assert html_to_text("<ul><li>Java</li><li>SQL</li></ul><p>a<br>b</p>") == "Java\nSQL\n\na\nb"


def test_build_career_sources():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    sites = [
        CareerSite(type="greenhouse", company="A", id="a"),
        CareerSite(type="lever", company="B", id="b"),
        CareerSite(type="teamtailor", company="C", id="c"),
    ]
    sources = build_career_sources(sites, http)
    assert [type(s) for s in sources] == [GreenhouseSource, LeverSource, TeamtailorSource]
    assert [s.name for s in sources] == [
        "careers:greenhouse:a",
        "careers:lever:b",
        "careers:teamtailor:c",
    ]
    assert build_career_sources([], http) == []
