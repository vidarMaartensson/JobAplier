import shutil
import time
from pathlib import Path

import pytest

from jobaplier.apply.base import SubmitError
from jobaplier.apply.form import FillReport, FormApplier, GenericAdapter
from jobaplier.apply.form import applier as applier_module
from jobaplier.config import Config, Paths
from jobaplier.models import ApplicationStatus, Channel, Draft, Job

FIXTURE = Path(__file__).parents[2] / "fixtures" / "forms" / "unknown.html"


@pytest.fixture(autouse=True)
def cv(paths: Paths, make_pdf) -> None:
    shutil.copy(make_pdf("Mitt CV"), paths.cv)


def make_job(url: str | None) -> Job:
    return Job(id=7, dedupe_key="k", title="Utvecklare", company="Exempel AB", apply_form_url=url)


def make_draft() -> Draft:
    return Draft(job_id=7, letter_text="Hej!\n\nJag söker tjänsten.")


@pytest.fixture
def make_applier(config: Config, paths: Paths):
    appliers: list[FormApplier] = []

    def _make(allow_file_urls: bool = True) -> FormApplier:
        applier = FormApplier(
            lambda: config,
            paths,
            [GenericAdapter()],
            headless=True,
            allow_file_urls=allow_file_urls,
        )
        appliers.append(applier)
        return applier

    yield _make
    for applier in appliers:
        applier.close()


def test_submit_returns_submitting_with_notes(make_applier, paths: Paths):
    applier = make_applier()
    result = applier.submit(make_job(FIXTURE.as_uri()), make_draft())
    assert result.channel == Channel.form
    assert result.status == ApplicationStatus.submitting
    assert result.dry_run is False
    assert "Löneanspråk" in result.notes
    # Brevet ligger kvar så att användaren kan ladda upp det i den öppna sidan.
    assert (paths.data_dir / "tmp" / "letter-7.pdf").is_file()
    applier.close()
    applier.close()  # idempotent


def test_submit_after_close_restarts_browser(make_applier):
    applier = make_applier()
    applier.submit(make_job(FIXTURE.as_uri()), make_draft())
    applier.close()
    result = applier.submit(make_job(FIXTURE.as_uri()), make_draft())
    assert "Löneanspråk" in result.notes


@pytest.mark.parametrize(
    "url",
    [None, "", "inte en url", "javascript:alert(1)", "ftp://exempel.se/a", "http://"],
)
def test_applier_error_becomes_submit_error(make_applier, paths: Paths, url):
    with pytest.raises(SubmitError):
        make_applier().submit(make_job(url), make_draft())
    assert list(paths.browser.iterdir()) == []  # webbläsaren startades aldrig


def test_file_url_rejected_unless_allowed(make_applier, paths: Paths):
    with pytest.raises(SubmitError):
        make_applier(allow_file_urls=False).submit(make_job(FIXTURE.as_uri()), make_draft())
    assert list(paths.browser.iterdir()) == []


def test_error_in_browser_thread_becomes_submit_error(make_applier, tmp_path: Path):
    applier = make_applier()
    with pytest.raises(SubmitError, match="^Kunde inte fylla i formuläret: [^\n]+$"):
        applier.submit(make_job((tmp_path / "saknas.html").as_uri()), make_draft())
    # Tråden lever vidare och klarar nästa uppdrag.
    assert "Löneanspråk" in applier.submit(make_job(FIXTURE.as_uri()), make_draft()).notes


def test_missing_cv_is_submit_error(make_applier, paths: Paths):
    paths.cv.unlink()
    with pytest.raises(SubmitError, match="CV"):
        make_applier().submit(make_job(FIXTURE.as_uri()), make_draft())


class SlowAdapter:
    """Blockerar arbetstråden för uppdrag 1 och noterar vilka sidor som öppnats."""

    name = "slow"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.open_urls: list[str] = []

    def matches(self, url: str, html: str) -> bool:
        return True

    def fill(self, page, applicant) -> FillReport:
        self.calls.append(page.url)
        if page.url.endswith("?job=1"):
            time.sleep(2)
        self.open_urls = [p.url for p in page.context.pages]
        return FillReport()


def test_timed_out_submit_is_never_opened_later(config: Config, paths: Paths, monkeypatch):
    adapter = SlowAdapter()
    applier = FormApplier(
        lambda: config, paths, [adapter], headless=True, allow_file_urls=True
    )
    url = FIXTURE.as_uri()
    try:
        applier.submit(make_job(url + "?job=0"), make_draft())  # webbläsaren startad
        monkeypatch.setattr(applier_module, "SUBMIT_TIMEOUT_S", 0.5)
        with pytest.raises(SubmitError, match="hann inte"):
            applier.submit(make_job(url + "?job=1"), make_draft())  # pågår i tråden
        with pytest.raises(SubmitError, match="hann inte"):
            applier.submit(make_job(url + "?job=2"), make_draft())  # köad bakom 1
        monkeypatch.setattr(applier_module, "SUBMIT_TIMEOUT_S", 30)
        applier.submit(make_job(url + "?job=3"), make_draft())
    finally:
        applier.close()
    assert adapter.calls == [url + "?job=0", url + "?job=1", url + "?job=3"]
    assert not any(u.endswith("?job=2") for u in adapter.open_urls)
