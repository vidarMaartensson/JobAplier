from pathlib import Path

import httpx
import pytest

from jobaplier.sources.linkedin_page import JOB_URL, JobPage, fetch_job_page, parse_job_page

JOB_PAGE = (Path(__file__).parent.parent / "fixtures" / "linkedin" / "job_page.html").read_text(
    encoding="utf-8"
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def no_real_sleep(monkeypatch):
    def fail(seconds):
        raise AssertionError(f"time.sleep({seconds}) anropades")

    monkeypatch.setattr("time.sleep", fail)


def test_job_url():
    assert JOB_URL.format(id="42") == "https://www.linkedin.com/jobs/view/42"


def test_parse_public_page():
    page = parse_job_page(JOB_PAGE)
    assert isinstance(page, JobPage)
    assert page.title == "Backendutvecklare Python"
    assert page.company == "Exempel Teknik AB"
    assert page.location == "Stockholm, Stockholms län, Sverige"
    assert "Om rollen" in page.description
    assert "Minst tre års erfarenhet av Python" in page.description
    assert "Visa mer" not in page.description


def test_parse_page_without_title_gives_none():
    assert parse_job_page("<html><body>Logga in</body></html>") is None


def test_parse_page_with_only_title():
    page = parse_job_page(
        '<html><body><h1 class="top-card-layout__title">Sidans titel</h1></body></html>'
    )
    assert page == JobPage(title="Sidans titel", company="", location=None, description="")


def test_fetch_public_page():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=JOB_PAGE)

    page = fetch_job_page(_client(handler), "3912345678")
    assert page is not None
    assert page.title == "Backendutvecklare Python"
    assert [str(r.url) for r in requests] == ["https://www.linkedin.com/jobs/view/3912345678"]


@pytest.mark.parametrize("status", [999, 500, 429, 404])
def test_unavailable_page_is_one_request_without_backoff(status, no_real_sleep):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    assert fetch_job_page(_client(handler), "1") is None
    assert len(requests) == 1


def test_transport_error_is_one_request_without_backoff(no_real_sleep):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("nere")

    assert fetch_job_page(_client(handler), "1") is None
    assert len(requests) == 1


def test_non_200_success_status_gives_none():
    assert fetch_job_page(_client(lambda r: httpx.Response(204)), "1") is None


def test_page_without_title_gives_none():
    login_wall = "<html><body>Logga in</body></html>"
    assert fetch_job_page(_client(lambda r: httpx.Response(200, text=login_wall)), "1") is None
