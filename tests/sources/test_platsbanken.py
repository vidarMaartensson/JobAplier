import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from jobaplier.config import Preferences
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import ApplyInfo, SourceError
from jobaplier.sources.platsbanken import PlatsbankenSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "platsbanken"
SINCE = datetime(2026, 10, 5, 8, 30, 15)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _source(prefs: Preferences, handler) -> PlatsbankenSource:
    return PlatsbankenSource(prefs, httpx.Client(transport=httpx.MockTransport(handler)))


def _paged_handler(requests: list[httpx.Request]):
    pages = {0: _load("page1.json"), 100: _load("page2.json")}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=pages.get(offset, {"total": {"value": 150}, "hits": []}))

    return handler


@pytest.fixture
def jobs():
    source = _source(Preferences(roles=["backend"]), _paged_handler([]))
    return {j.external_id: j for j in source.fetch(SINCE)}


def test_maps_fields(jobs):
    hit = _load("page1.json")["hits"][0]
    job = jobs[hit["id"]]
    assert job.source == "platsbanken"
    assert job.external_id == hit["id"]
    assert job.title == hit["headline"] == "Backendutvecklare"
    assert job.company == hit["employer"]["name"] == "Exempel AB"
    assert job.location == "Stockholm"
    assert job.description == hit["description"]["text"]
    assert job.url == hit["webpage_url"]
    assert job.published_at == datetime(2026, 10, 12, 8, 30, 0)
    assert job.published_at.tzinfo is None
    assert job.remote is None
    assert job.incomplete is False


def test_location_falls_back_to_region(jobs):
    assert jobs["30000004"].location == "Skåne län"


def test_email_apply(jobs):
    assert jobs["30000001"].apply == ApplyInfo(
        method=ApplyMethod.email, email="jobb@exempel.se", reference="BE-2026-17"
    )


def test_ats_url_is_form(jobs):
    apply = jobs["30000002"].apply
    assert apply.method == ApplyMethod.form
    assert apply.form_url == "https://exempel.teamtailor.com/jobs/123-fullstackutvecklare"
    assert apply.email is None


def test_no_apply_info_is_unknown(jobs):
    job = jobs["30000003"]
    assert job.apply == ApplyInfo(method=ApplyMethod.unknown)
    assert job.url == "https://arbetsformedlingen.se/platsbanken/annonser/30000003"


def test_unknown_apply_url_is_unknown(jobs):
    job = jobs["30000004"]
    assert job.apply.method == ApplyMethod.unknown
    assert job.apply.form_url is None
    assert job.url == "https://arbetsformedlingen.se/platsbanken/annonser/30000004"


def test_paging_and_since():
    requests: list[httpx.Request] = []
    source = _source(Preferences(roles=["backend"]), _paged_handler(requests))
    result = source.fetch(SINCE)
    assert [int(r.url.params["offset"]) for r in requests] == [0, 100]  # 200 >= total 150
    for r in requests:
        assert r.url.params["limit"] == "100"
        assert r.url.params["q"] == "backend"
        assert r.url.params["published-after"] == "2026-10-05T08:30:15"
        assert str(r.url).startswith("https://jobsearch.api.jobtechdev.se/search?")
    assert [j.external_id for j in result] == [f"3000000{i}" for i in range(1, 7)]


def test_paging_never_exceeds_max_offset():
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(int(request.url.params["offset"]))
        return httpx.Response(200, json={"total": {"value": 99999}, "hits": [_load("page2.json")["hits"][0]]})

    _source(Preferences(roles=["backend"]), handler).fetch(SINCE)
    assert offsets == list(range(0, 2001, 100))


def test_stops_on_empty_page():
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(int(request.url.params["offset"]))
        return httpx.Response(200, json={"total": {"value": 500}, "hits": []})

    assert _source(Preferences(roles=["backend"]), handler).fetch(SINCE) == []
    assert offsets == [0]


def test_one_query_per_role_merged():
    requests: list[httpx.Request] = []
    source = _source(Preferences(roles=["backend", "fullstack"]), _paged_handler(requests))
    result = source.fetch(SINCE)
    assert [r.url.params["q"] for r in requests] == ["backend", "backend", "fullstack", "fullstack"]
    ids = [j.external_id for j in result]
    assert len(ids) == len(set(ids)) == 6


def test_keywords_used_when_no_roles():
    requests: list[httpx.Request] = []
    source = _source(Preferences(keywords=["python"]), _paged_handler(requests))
    source.fetch(SINCE)
    assert {r.url.params["q"] for r in requests} == {"python"}


def test_no_roles_or_keywords_makes_no_requests():
    requests: list[httpx.Request] = []
    assert _source(Preferences(), _paged_handler(requests)).fetch(SINCE) == []
    assert requests == []


def test_http_failure_raises_source_error():
    source = _source(Preferences(roles=["backend"]), lambda r: httpx.Response(404))
    with pytest.raises(SourceError):
        source.fetch(SINCE)


def test_missing_optional_fields_tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        hit = {"id": 42, "headline": "X", "employer": None, "workplace_address": None,
               "description": None, "webpage_url": None, "publication_date": None,
               "application_details": None}
        return httpx.Response(200, json={"total": {"value": 1}, "hits": [hit]})

    [job] = _source(Preferences(roles=["x"]), handler).fetch(SINCE)
    assert (job.external_id, job.company, job.location, job.description, job.url) == ("42", "", None, "", "")
    assert job.published_at is None
    assert job.apply.method == ApplyMethod.unknown
