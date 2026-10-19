from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fakes import FakeGmail

from jobaplier.gmail import GmailAuthError, GmailError, Mail
from jobaplier.models import ApplyMethod
from jobaplier.sources.base import SourceError
from jobaplier.sources.linkedin_alerts import LinkedInAlertsSource, extract_job_refs

FIXTURES = Path(__file__).parent.parent / "fixtures" / "linkedin"
ALERT_HTML = (FIXTURES / "alert.html").read_text(encoding="utf-8")
JOB_PAGE = (FIXTURES / "job_page.html").read_text(encoding="utf-8")
SINCE = datetime(2026, 10, 5, 8, 30, 15, tzinfo=UTC)
RECEIVED = datetime(2026, 10, 18, 6, 0, 0)


def _mail(html: str = ALERT_HTML, mail_id: str = "m1", received_at: datetime = RECEIVED) -> Mail:
    return Mail(
        id=mail_id,
        sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        subject="Backendutvecklare: 3 nya jobb i Stockholm",
        received_at=received_at,
        html=html,
        text="",
    )


def _source(gmail, handler, sleeps: list[float] | None = None) -> LinkedInAlertsSource:
    return LinkedInAlertsSource(
        gmail,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _page_handler(requests: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, text=JOB_PAGE)

    return handler


def test_extract_unique_refs():
    refs = extract_job_refs(ALERT_HTML)
    assert refs == [
        ("3912345678", "Backendutvecklare Python", "Exempel Teknik AB"),
        ("3923456789", "Systemutvecklare – Backend", "Åkeri & Data Exempel AB"),
        ("3934567890", "Senior Software Engineer", "Sample Systems"),
    ]


def test_extract_ignores_non_job_links_and_missing_company():
    html = (
        '<a href="https://www.linkedin.com/comm/feed/">Flöde</a>'
        '<a href="https://www.linkedin.com/jobs/view/42/">Titel</a>'
        '<a href="https://www.linkedin.com/jobs/view/43/">Nästa titel</a>'
    )
    # Inget bolag mellan två jobblänkar: tom sträng, inte nästa korts titel.
    assert extract_job_refs(html) == [("42", "Titel", ""), ("43", "Nästa titel", "")]


def test_fetches_public_page():
    requests: list[httpx.Request] = []
    gmail = FakeGmail([_mail()])
    jobs = _source(gmail, _page_handler(requests)).fetch(SINCE)

    assert [j.external_id for j in jobs] == ["3912345678", "3923456789", "3934567890"]
    assert [str(r.url) for r in requests] == [
        f"https://www.linkedin.com/jobs/view/{job.external_id}" for job in jobs
    ]
    job = jobs[0]
    assert job.source == "linkedin_alerts"
    assert job.incomplete is False
    assert job.title == "Backendutvecklare Python"
    assert job.company == "Exempel Teknik AB"
    assert job.location == "Stockholm, Stockholms län, Sverige"
    assert job.remote is None
    assert job.url == "https://www.linkedin.com/jobs/view/3912345678"
    assert job.published_at == RECEIVED
    assert job.apply.method is ApplyMethod.unknown
    assert "Om rollen" in job.description
    assert "Minst tre års erfarenhet av Python" in job.description
    assert "Visa mer" not in job.description


def test_blocked_page_gives_incomplete():
    gmail = FakeGmail([_mail()])
    source = _source(gmail, lambda request: httpx.Response(999, text="blocked"))
    jobs = source.fetch(SINCE)

    assert len(jobs) == 3
    job = jobs[0]
    assert job.incomplete is True
    assert job.title == "Backendutvecklare Python"
    assert job.company == "Exempel Teknik AB"
    assert job.description == ""
    assert job.location is None
    assert job.url == "https://www.linkedin.com/jobs/view/3912345678"
    assert job.published_at == RECEIVED


@pytest.fixture
def no_real_sleep(monkeypatch):
    def fail(seconds):
        raise AssertionError(f"time.sleep({seconds}) anropades")

    monkeypatch.setattr("time.sleep", fail)


@pytest.mark.parametrize("status", [999, 500, 429, 404])
def test_blocked_page_is_one_request_without_backoff(status, no_real_sleep):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    jobs = _source(FakeGmail([_mail()]), handler).fetch(SINCE)
    assert len(jobs) == 3
    assert len(requests) == 3  # exakt en förfrågan per jobb
    assert all(j.incomplete for j in jobs)


def test_transport_error_is_one_request_without_backoff(no_real_sleep):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("nere")

    jobs = _source(FakeGmail([_mail()]), handler).fetch(SINCE)
    assert len(requests) == 3
    assert all(j.incomplete for j in jobs)


def test_source_error_from_page_gives_incomplete():
    # get_with_retry kastar SourceError direkt vid 404.
    source = _source(FakeGmail([_mail()]), lambda request: httpx.Response(404))
    jobs = source.fetch(SINCE)
    assert [j.incomplete for j in jobs] == [True, True, True]


def test_page_without_title_gives_incomplete():
    login_wall = "<html><body>Logga in</body></html>"
    source = _source(FakeGmail([_mail()]), lambda r: httpx.Response(200, text=login_wall))
    job = source.fetch(SINCE)[1]
    assert job.incomplete is True
    assert job.title == "Systemutvecklare – Backend"
    assert job.company == "Åkeri & Data Exempel AB"
    assert job.description == ""


def test_page_values_fall_back_to_mail_values():
    page = '<html><body><h1 class="top-card-layout__title">Sidans titel</h1></body></html>'
    source = _source(FakeGmail([_mail()]), lambda r: httpx.Response(200, text=page))
    job = source.fetch(SINCE)[0]
    assert job.incomplete is False
    assert job.title == "Sidans titel"
    assert job.company == "Exempel Teknik AB"
    assert job.location is None


def test_query_uses_since():
    gmail = FakeGmail([])
    assert _source(gmail, _page_handler()).fetch(SINCE) == []
    assert gmail.queries == [
        f"from:jobalerts-noreply@linkedin.com after:{int(SINCE.timestamp())}"
    ]


def test_naive_since_is_treated_as_utc():
    gmail = FakeGmail([])
    _source(gmail, _page_handler()).fetch(SINCE.replace(tzinfo=None))
    assert gmail.queries[0].endswith(f"after:{int(SINCE.timestamp())}")


def test_same_job_in_several_mails_is_fetched_once():
    requests: list[httpx.Request] = []
    older = datetime(2026, 10, 17, 6, 0, 0)
    gmail = FakeGmail([_mail(mail_id="m1"), _mail(mail_id="m2", received_at=older)])
    jobs = _source(gmail, _page_handler(requests)).fetch(SINCE)
    assert len(jobs) == 3
    assert len(requests) == 3
    assert jobs[0].published_at == RECEIVED


def test_sleeps_between_page_fetches_only():
    sleeps: list[float] = []
    _source(FakeGmail([_mail()]), _page_handler(), sleeps).fetch(SINCE)
    assert sleeps == [1.0, 1.0]


def test_no_sleep_for_single_job():
    html = '<a href="https://www.linkedin.com/jobs/view/42/">Titel</a><p>Bolaget</p>'
    sleeps: list[float] = []
    _source(FakeGmail([_mail(html)]), _page_handler(), sleeps).fetch(SINCE)
    assert sleeps == []


def test_auth_error_becomes_source_error():
    class Broken(FakeGmail):
        def search(self, query):
            raise GmailAuthError("token ogiltig")

    with pytest.raises(SourceError) as excinfo:
        _source(Broken(), _page_handler()).fetch(SINCE)
    assert "jobaplier gmail-login" in str(excinfo.value)
    assert str(excinfo.value) == "Gmail-inloggningen har gått ut – kör `jobaplier gmail-login`"


def test_gmail_error_becomes_swedish_source_error():
    class Broken(FakeGmail):
        def search(self, query):
            raise GmailError("quota")

    with pytest.raises(SourceError, match="Gmail"):
        _source(Broken(), _page_handler()).fetch(SINCE)
