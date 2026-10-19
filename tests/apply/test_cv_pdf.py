from pathlib import Path

import pytest

from jobaplier.apply.cv_pdf import HEADINGS, render_cv_pdf
from jobaplier.apply.tailor import RenderedCV, RenderedEducation, RenderedExperience
from jobaplier.profile.cv import extract_cv_text
from jobaplier.profile.cv_document import OtherSection


def make_cv(**overrides) -> RenderedCV:
    values = dict(
        language="sv",
        name="Vidar Mårtensson",
        email="vidar@example.com",
        phone="070-123 45 67",
        location="Göteborg",
        linkedin_url="https://www.linkedin.com/in/vidar",
        headline="Backendutvecklare",
        summary="Erfaren utvecklare med fokus på Python.",
        experience=[
            RenderedExperience(
                title="Systemutvecklare",
                employer="Exempel AB",
                location="Stockholm",
                period="2020 – nu",
                bullets=["Byggde ett API i Python"],
            )
        ],
        education=[
            RenderedEducation(
                degree="Civilingenjör",
                school="KTH",
                period="2010 – 2015",
                details=["Inriktning datateknik"],
            )
        ],
        skills=["Python", "SQL"],
        languages=["Svenska", "Engelska"],
        other=[OtherSection(heading="Certifieringar", items=["AWS Solutions Architect"])],
    )
    values.update(overrides)
    return RenderedCV(**values)


def text_of(cv: RenderedCV, tmp_path: Path) -> str:
    return extract_cv_text(render_cv_pdf(cv, tmp_path / "cv.pdf"))


def test_renders_pdf_with_content(tmp_path: Path):
    out = tmp_path / "sub" / "cv.pdf"
    assert render_cv_pdf(make_cv(), out) == out
    assert out.read_bytes().startswith(b"%PDF")
    text = extract_cv_text(out)
    for expected in (
        "Vidar Mårtensson",
        "Backendutvecklare",
        "Systemutvecklare",
        "Exempel AB",
        "Byggde ett API i Python",
        "Civilingenjör",
        "Certifieringar",
        "AWS Solutions Architect",
        "vidar@example.com",
    ):
        assert expected in text


def test_headings_follow_language(tmp_path: Path):
    sv = text_of(make_cv(), tmp_path)
    for heading in ("Profil", "Erfarenhet", "Utbildning", "Kompetenser", "Språk"):
        assert heading in sv
    en = text_of(make_cv(language="en"), tmp_path)
    for heading in ("Profile", "Experience", "Education", "Skills", "Languages"):
        assert heading in en
    assert "Erfarenhet" not in en
    assert HEADINGS["sv"]["experience"] == "Erfarenhet"
    assert HEADINGS["en"]["languages"] == "Languages"


def test_escapes_html(tmp_path: Path):
    cv = make_cv(
        summary="<b>fet</b> & mer",
        experience=[
            RenderedExperience(
                title="Dev",
                employer="X",
                location="",
                period="",
                bullets=["<script>x</script>"],
            )
        ],
        other=[OtherSection(heading="<i>Egen</i>", items=["a"])],
    )
    text = text_of(cv, tmp_path)
    assert "<script>x</script>" in text
    assert "<b>fet</b> & mer" in text
    assert "<i>Egen</i>" in text


def test_empty_sections_hidden(tmp_path: Path):
    cv = make_cv(
        summary="",
        experience=[],
        education=[],
        skills=[],
        languages=[],
        other=[],
        email="",
        phone="",
        location="",
        linkedin_url="",
    )
    text = text_of(cv, tmp_path)
    assert "Vidar Mårtensson" in text
    for heading in ("Profil", "Erfarenhet", "Utbildning", "Kompetenser", "Språk"):
        assert heading not in text
    assert "·" not in text


def test_contact_row_only_non_empty_parts(tmp_path: Path):
    text = text_of(make_cv(phone="", linkedin_url=""), tmp_path)
    assert "vidar@example.com · Göteborg" in " ".join(text.split())


def test_other_section_without_heading_or_items_hidden(tmp_path: Path):
    cv = make_cv(other=[OtherSection(heading="Tom", items=[])])
    assert "Tom" not in text_of(cv, tmp_path).split()


def test_multiline_bullet_is_collapsed(tmp_path: Path):
    cv = make_cv(
        experience=[
            RenderedExperience(
                title="Dev",
                employer="X",
                location="",
                period="",
                bullets=["Första raden\n   andra raden"],
            )
        ]
    )
    assert "Första raden andra raden" in " ".join(text_of(cv, tmp_path).split())


def test_template_is_packaged():
    import jobaplier.apply.cv_pdf as module

    assert (Path(module.__file__).parent / "templates" / "cv.html").is_file()


@pytest.mark.parametrize("language", ["sv", "en"])
def test_long_cv_fits_two_pages(tmp_path: Path, language: str):
    from pypdf import PdfReader

    experience = [
        RenderedExperience(
            title=f"Roll {i}",
            employer=f"Företag {i} AB",
            location="Stockholm",
            period=f"20{10 + i} – 20{12 + i}",
            bullets=["Levererade en förbättring som minskade svarstiden för kunderna."] * 4,
        )
        for i in range(4)
    ]
    out = render_cv_pdf(make_cv(language=language, experience=experience), tmp_path / "cv.pdf")
    assert len(PdfReader(out).pages) <= 2
