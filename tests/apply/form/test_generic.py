from pathlib import Path

import pytest
from playwright.sync_api import Page

from jobaplier.apply.form import (
    Applicant,
    FillReport,
    GenericAdapter,
    classify_field,
    highlight,
)
from jobaplier.config import Config, Paths

FIXTURE = Path(__file__).parents[2] / "fixtures" / "forms" / "unknown.html"
CONSENT = "Jag godkänner villkoren och behandlingen av mina personuppgifter"
LETTER = "Hej!\n\nJag söker tjänsten som utvecklare."

OUTLINE_JS = """e => {
    const s = getComputedStyle(e);
    return [s.outlineStyle, s.outlineWidth, s.outlineColor];
}"""


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


@pytest.fixture
def filled(page: Page, applicant: Applicant) -> FillReport:
    page.goto(FIXTURE.as_uri())
    return GenericAdapter().fill(page, applicant)


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        ("Förnamn *", "first_name"),
        ("E-postadress", "email"),
        ("Bifoga CV", "cv"),
        ("Cover letter", "letter"),
        ("Löneanspråk", None),
        ("Namn", "full_name"),
        ("Efternamn", "last_name"),
        ("LinkedIn-profil", "linkedin"),
        ("Mobilnummer", "phone"),
    ],
)
def test_classify_field(descriptor: str, expected: str | None):
    assert classify_field(descriptor) == expected


def test_applicant_from_config(config: Config, paths: Paths, tmp_path: Path):
    config.profile.name = "Anna Maria Svensson"
    config.profile.phone = "070-1"
    a = Applicant.from_config(config, paths, "Brev", tmp_path / "l.pdf")
    assert (a.first_name, a.last_name) == ("Anna", "Maria Svensson")
    assert (a.email, a.phone, a.cv_path) == ("test@example.se", "070-1", paths.cv)
    assert (a.letter_text, a.letter_pdf) == ("Brev", tmp_path / "l.pdf")

    config.profile.name = "Cher"
    a = Applicant.from_config(config, paths, "Brev", tmp_path / "l.pdf")
    assert (a.first_name, a.last_name) == ("Cher", "")


def test_generic_fills_known_fields(page: Page, filled: FillReport):
    assert page.input_value("#first") == "Test"
    assert page.input_value("#last") == "Testsson"
    assert page.input_value("#email") == "test@example.se"
    assert page.input_value("#phone") == "070-123 45 67"
    assert page.input_value("#linkedin") == "https://www.linkedin.com/in/testtestsson"
    assert page.eval_on_selector("#cv", "e => e.files[0].name") == "cv.pdf"
    assert page.input_value("#letter") == LETTER
    assert filled.filled == [
        "Förnamn",
        "Efternamn",
        "E-postadress",
        "Telefon",
        "LinkedIn-profil",
        "Bifoga CV",
        "Personligt brev",
    ]


def test_unanswered_reported_and_highlighted(page: Page, filled: FillReport):
    assert filled.unanswered == ["Löneanspråk", CONSENT]
    for selector in ("#salary", "#consent"):
        assert page.eval_on_selector(selector, OUTLINE_JS) == [
            "solid",
            "3px",
            "rgb(225, 29, 72)",
        ]
    # Ifyllda och dolda fält markeras inte.
    for selector in ("#first", "#cv", "#website"):
        assert page.eval_on_selector(selector, OUTLINE_JS)[0] == "none"


def test_never_submits(page: Page, applicant: Applicant):
    page.goto(FIXTURE.as_uri())
    url = page.url
    GenericAdapter().fill(page, applicant)
    assert page.evaluate("window.__submitted === true") is False
    assert page.url == url


def test_consent_untouched(page: Page, filled: FillReport):
    assert page.is_checked("#consent") is False


def test_full_name_select_and_letter_file(page: Page, applicant: Applicant):
    page.set_content(
        """
        <label for="n">Namn</label><input id="n" name="name">
        <label for="s">Ort *</label>
        <select id="s" name="city" required>
          <option value="">Välj</option><option>Stockholm</option>
        </select>
        <label for="l">Personligt brev (PDF)</label><input id="l" type="file">
        <label for="h">Hur hittade du oss?</label><input id="h" name="source">
        <input id="opt" name="nickname" placeholder="Smeknamn">
        """
    )
    report = GenericAdapter().fill(page, applicant)
    assert page.input_value("#n") == "Test Testsson"
    assert page.input_value("#s") == ""
    assert page.eval_on_selector("#l", "e => e.files[0].name") == "letter.pdf"
    assert report.filled == ["Namn", "Personligt brev (PDF)"]
    # Obligatoriska och oklassade tomma fält; etikett, annars placeholder.
    assert report.unanswered == ["Ort", "Hur hittade du oss?", "Smeknamn"]


def test_existing_values_kept(page: Page, applicant: Applicant):
    page.set_content('<label for="e">E-post</label><input id="e" value="egen@x.se">')
    report = GenericAdapter().fill(page, applicant)
    assert page.input_value("#e") == "egen@x.se"
    assert report == FillReport(filled=[], unanswered=[])


def test_highlight_without_selectors_is_noop(page: Page):
    page.set_content("<input id='a'>")
    highlight(page, [])
    assert page.eval_on_selector("#a", "e => getComputedStyle(e).outlineStyle") == "none"
    assert page.evaluate("document.querySelectorAll('style').length") == 0


AUTOSUBMIT = FIXTURE.with_name("autosubmit.html")


def test_page_handlers_cannot_submit_during_fill(page: Page, applicant: Applicant):
    page.goto(AUTOSUBMIT.as_uri())
    url = page.url
    report = GenericAdapter().fill(page, applicant)
    assert report.filled == ["Förnamn", "E-post"]
    assert page.evaluate("window.__submitted === true") is False
    assert page.url == url
    # Skydden är borttagna efteråt: användaren kan fortfarande skicka själv.
    with page.expect_navigation():
        page.evaluate("document.getElementById('application').requestSubmit()")
    assert page.url.startswith(url + "?")
    assert "first_name=Test" in page.url


def test_no_fields_found_note(page: Page, applicant: Applicant):
    page.set_content("<h1>Logga in för att ansöka</h1>")
    report = GenericAdapter().fill(page, applicant)
    assert report == FillReport(
        filled=[], unanswered=["Inga formulärfält hittades – fyll i manuellt"]
    )
