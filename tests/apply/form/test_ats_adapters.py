from pathlib import Path

import pytest
from playwright.sync_api import Page

from jobaplier.apply.form import (
    Applicant,
    FillReport,
    FormAdapter,
    GenericAdapter,
    ReachMeeAdapter,
    TeamtailorAdapter,
    VarbiAdapter,
    default_adapters,
)

FORMS = Path(__file__).parents[2] / "fixtures" / "forms"
LETTER = "Hej!\n\nJag söker tjänsten som utvecklare."

# (adapter, fixture, ATS-URL, CV-väljare, brev-väljare, ifyllda etiketter, obesvarade)
CASES = {
    "teamtailor": (
        TeamtailorAdapter,
        "teamtailor.html",
        "https://exempel.teamtailor.com/jobs/1-x/applications/new",
        "input.dz-hidden-input >> nth=0",
        "input.dz-hidden-input >> nth=1",
    ),
    "varbi": (
        VarbiAdapter,
        "varbi.html",
        "https://exempel.varbi.com/se/what:apply/jobID:1/",
        "#attachment_cv",
        "#attachment_letter",
    ),
    "reachmee": (
        ReachMeeAdapter,
        "reachmee.html",
        "https://web103.reachmee.com/ext/I005/1/main?job_id=1",
        "#file_cv",
        "#file_letter",
    ),
}
ALL = pytest.mark.parametrize("ats", CASES)

TEXT_FIELDS = {
    "teamtailor": {
        "#candidate_first_name": "Test",
        "#candidate_last_name": "Testsson",
        "#candidate_email": "test@example.se",
        "#candidate_phone": "070-123 45 67",
    },
    "varbi": {
        "#firstname": "Test",
        "#lastname": "Testsson",
        "#email": "test@example.se",
        "#phone": "070-123 45 67",
    },
    "reachmee": {
        "#applicant_first_name": "Test",
        "#applicant_last_name": "Testsson",
        "#applicant_email": "test@example.se",
        "#applicant_mobile": "070-123 45 67",
    },
}
CONSENT = {
    "teamtailor": ["#candidate_consent_given", "#candidate_consent_given_future_jobs"],
    "varbi": ["#gdpr"],
    "reachmee": ["#rm_consent"],
}
CORE_LABELS = ["Förnamn", "Efternamn", "E-post", "Telefon", "CV", "Personligt brev"]


@pytest.fixture
def applicant(tmp_path: Path) -> Applicant:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4\n")
    letter_pdf = tmp_path / "letter.pdf"
    letter_pdf.write_bytes(b"%PDF-1.4\n")
    return Applicant(
        first_name="Test",
        last_name="Testsson",
        email="test@example.se",
        phone="070-123 45 67",
        linkedin_url="https://www.linkedin.com/in/testtestsson",
        cv_path=cv,
        letter_text=LETTER,
        letter_pdf=letter_pdf,
    )


def _adapter(ats: str) -> FormAdapter:
    return CASES[ats][0]()


def _fixture(ats: str) -> Path:
    return FORMS / CASES[ats][1]


def _go(page: Page, ats: str) -> None:
    page.goto(_fixture(ats).as_uri())


@ALL
def test_matches_by_url_or_html(ats: str):
    adapter = _adapter(ats)
    html = _fixture(ats).read_text()
    assert adapter.name == ats
    assert adapter.matches(CASES[ats][2], "")  # känd domän räcker
    assert adapter.matches("file:///tmp/x.html", html)  # HTML-signatur räcker
    assert not adapter.matches("https://example.com/jobs/1", "<form></form>")
    assert not adapter.matches(
        "file:///tmp/x.html", (FORMS / "unknown.html").read_text()
    )
    for other in set(CASES) - {ats}:
        assert not adapter.matches("file:///tmp/x.html", _fixture(other).read_text())


def test_teamtailor_matches_custom_domain_html():
    # Teamtailor-sajter ligger ofta på företagets egen domän (career.foretag.se).
    html = '<form id="job-application-form"><input name="candidate[first_name]"></form>'
    assert TeamtailorAdapter().matches("https://karriar.foretag.se/jobs/1", html)


@ALL
def test_fills_core_fields(page: Page, applicant: Applicant, ats: str):
    _go(page, ats)
    report = _adapter(ats).fill(page, applicant)
    for selector, value in TEXT_FIELDS[ats].items():
        assert page.input_value(selector) == value
    cv_selector, letter_selector = CASES[ats][3], CASES[ats][4]
    # Filfälten är dolda men får ändå sina filer.
    if ats != "reachmee":  # ReachMee-fixturen gömmer fälten utanför bild i stället
        assert not page.locator(cv_selector).is_visible()
    assert page.eval_on_selector(cv_selector, "e => e.files[0].name") == "cv.pdf"
    assert page.eval_on_selector(letter_selector, "e => e.files[0].name") == "letter.pdf"
    assert report.filled == CORE_LABELS


@ALL
def test_leaves_consent_and_reports_unanswered(page: Page, applicant: Applicant, ats: str):
    _go(page, ats)
    report = _adapter(ats).fill(page, applicant)
    for selector in CONSENT[ats]:
        assert page.is_checked(selector) is False
    assert len(report.unanswered) == len(set(report.unanswered))
    assert len(report.unanswered) == 1 + len(CONSENT[ats])  # lön/titel + samtycken
    assert not set(report.filled) & set(report.unanswered)
    # Obesvarade fält är markerade.
    assert page.locator("[data-jobaplier-unanswered]").count() == len(report.unanswered)


@ALL
def test_never_submits_or_opens_file_dialogs(page: Page, applicant: Applicant, ats: str):
    _go(page, ats)
    url = page.url
    dialogs: list[object] = []
    page.on("filechooser", lambda chooser: dialogs.append(chooser))
    # Som i riktiga uppladdningskomponenter öppnar ett klick på den stylade knappen
    # filväljaren; ett klick från adaptern skulle alltså synas som ett filechooser-event.
    page.evaluate(
        """() => {
            const open = () => document.querySelector("input[type=file]").click();
            document
                .querySelectorAll("button[type=button], [data-forms--inputs--upload-target=trigger]")
                .forEach((el) => el.addEventListener("click", open));
        }"""
    )
    _adapter(ats).fill(page, applicant)
    assert page.evaluate("window.__submitted === true") is False
    assert page.url == url
    assert dialogs == []  # den stylade knappen klickas aldrig
    assert page.evaluate("window.__jobaplierGuard === undefined")


@ALL
def test_page_handlers_cannot_submit_during_fill(page: Page, applicant: Applicant, ats: str):
    _go(page, ats)
    page.evaluate(
        """() => {
            const form = document.forms[0];
            for (const el of form.querySelectorAll("input[type=text], input[type=email], input[type=tel]")) {
                el.addEventListener("input", () => form.submit());
                el.addEventListener("change", () => form.requestSubmit());
            }
            form.addEventListener("submit", () => { window.__submitted = true; });
        }"""
    )
    url = page.url
    _adapter(ats).fill(page, applicant)
    assert page.evaluate("window.__submitted === true") is False
    assert page.url == url
    # Skydden är borta efteråt: användaren kan skicka själv.
    assert page.evaluate(
        "[HTMLFormElement.prototype.submit, HTMLFormElement.prototype.requestSubmit]"
        ".every((f) => f.toString().includes('[native code]'))"
    )


@ALL
def test_existing_values_kept(page: Page, applicant: Applicant, ats: str):
    _go(page, ats)
    selector = next(iter(TEXT_FIELDS[ats]))
    page.fill(selector, "Redan")
    report = _adapter(ats).fill(page, applicant)
    assert page.input_value(selector) == "Redan"
    assert "Förnamn" not in report.filled


def test_generic_report_merged_for_remaining_fields(page: Page, applicant: Applicant):
    # Okända fält som generiska adaptern känner igen fylls i efter de kända.
    _go(page, "varbi")
    page.evaluate(
        """() => {
            const row = document.createElement("div");
            row.innerHTML = '<label for="li">LinkedIn-profil</label><input id="li" type="url">';
            document.getElementById("salary").parentElement.after(row);
        }"""
    )
    report = VarbiAdapter().fill(page, applicant)
    assert page.input_value("#li") == "https://www.linkedin.com/in/testtestsson"
    assert report.filled == [*CORE_LABELS, "LinkedIn-profil"]


def test_teamtailor_without_matching_dropzones_leaves_files_unanswered(
    page: Page, applicant: Applicant
):
    _go(page, "teamtailor")
    page.evaluate("document.querySelector('input.dz-hidden-input').remove()")
    report = TeamtailorAdapter().fill(page, applicant)
    assert page.eval_on_selector("input.dz-hidden-input", "e => e.files.length") == 0
    assert report.filled == ["Förnamn", "Efternamn", "E-post", "Telefon"]
    assert "CV" in report.unanswered
    assert "Personligt brev" in report.unanswered


def test_teamtailor_cover_letter_textarea_when_no_letter_upload(
    page: Page, applicant: Applicant
):
    _go(page, "teamtailor")
    page.evaluate(
        """() => {
            document.getElementById("upload_cover_letter_field").remove();
            document.querySelectorAll("input.dz-hidden-input")[1].remove();
            const area = document.createElement("textarea");
            area.name = "candidate[pitch]";
            area.id = "candidate_pitch";
            document.getElementById("upload_resume_field").after(area);
        }"""
    )
    report = TeamtailorAdapter().fill(page, applicant)
    assert page.input_value("#candidate_pitch") == LETTER
    assert report.filled == CORE_LABELS


def test_absent_optional_fields_are_not_reported(page: Page, applicant: Applicant):
    page.set_content(
        """<form id="applyform"><meta name="csrf-param" content="_varbi_csrf_token_x">
        <input name="firstname" type="text"></form>"""
    )
    report = VarbiAdapter().fill(page, applicant)
    assert report == FillReport(filled=["Förnamn"], unanswered=[])


def test_default_adapters_order():
    adapters = default_adapters()
    assert [type(a) for a in adapters] == [
        TeamtailorAdapter,
        VarbiAdapter,
        ReachMeeAdapter,
        GenericAdapter,
    ]
    assert [a.name for a in adapters] == ["teamtailor", "varbi", "reachmee", "generic"]


def _pick(url: str, html: str) -> str:
    return next(a.name for a in default_adapters() if a.matches(url, html))


def test_generic_used_for_unknown():
    html = (FORMS / "unknown.html").read_text()
    assert _pick("https://exempel.se/jobb/1/ansok", html) == "generic"
    assert _pick("file:///x/unknown.html", html) == "generic"


@ALL
def test_default_adapters_pick_specific_adapter(ats: str):
    assert _pick("file:///x.html", _fixture(ats).read_text()) == ats
    assert _pick(CASES[ats][2], "") == ats
