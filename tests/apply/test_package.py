import shutil
from pathlib import Path

import pytest
from fakes import FakeAI

from jobaplier.ai import AIError
from jobaplier.apply import package as package_module
from jobaplier.apply.package import (
    BODY_EN,
    BODY_SV,
    application_subject,
    build_cv_pdf,
    email_body,
    mailto_link,
    valid_single_address,
)
from jobaplier.apply.tailor import TailoredCV, TailoredExperience
from jobaplier.config import Config, Paths
from jobaplier.db import session_scope
from jobaplier.models import Draft, Job, TailoredCVRecord
from jobaplier.profile.cv import CVMissing, cv_file_hash, extract_cv_text
from jobaplier.profile.cv_document import CVDocument, Experience


def make_job(**overrides) -> Job:
    values = dict(dedupe_key="k", title="Utvecklare", company="Exempel AB")
    values.update(overrides)
    return Job(**values)


def make_draft(language: str = "sv") -> Draft:
    return Draft(job_id=1, letter_text="Hej!", language=language)


def test_subject():
    assert application_subject(make_job()) == "Ansökan: Utvecklare"


def test_subject_with_reference():
    subject = application_subject(make_job(apply_reference="ABC-123"))
    assert subject == "Ansökan: Utvecklare (ref. ABC-123)"


def test_subject_is_one_line():
    job = make_job(title="Utvecklare\nBcc: x@y.se", apply_reference="A\r\nB")
    assert application_subject(job) == "Ansökan: Utvecklare Bcc: x@y.se (ref. A B)"


def test_swedish_body(config: Config):
    config.profile.phone = "070-123 45 67"
    assert email_body(make_job(), make_draft(), config) == (
        "Hej!\n\nHär kommer min ansökan till tjänsten Utvecklare. "
        "CV och personligt brev bifogas.\n\nVänliga hälsningar\n"
        "Test Testsson\n070-123 45 67"
    )


def test_english_body(config: Config):
    config.profile.phone = "+46 70 123 45 67"
    body = email_body(make_job(title="Engineer"), make_draft("en"), config)
    assert body == (
        "Hello,\n\nPlease find attached my application for the Engineer position, "
        "including my CV and cover letter.\n\nBest regards\nTest Testsson\n"
        "+46 70 123 45 67"
    )


def test_body_templates_are_exported():
    assert BODY_SV.startswith("Hej!")
    assert BODY_EN.startswith("Hello,")


def test_body_title_is_one_line(config: Config):
    body = email_body(make_job(title="Utvecklare\nBcc: x@y.se"), make_draft(), config)
    assert "tjänsten Utvecklare Bcc: x@y.se." in body


@pytest.mark.parametrize("address", ["jobb@exempel.se", "fornamn.efternamn@sub.exempel.se"])
def test_valid_single_address(address: str):
    assert valid_single_address(address) is True


@pytest.mark.parametrize(
    "address",
    ["a@b.se\nBcc: x@y.se", "a@b.se, c@d.se", "a@b.se;c@d.se", "inte en adress",
     "Namn <a@b.se>", "a@b", ""],
)
def test_invalid_single_address(address: str):
    assert valid_single_address(address) is False


def test_package_mailto_encoding():
    link = mailto_link("jobb@exempel.se", "Ansökan: Rör & VVS", "Hej!\nRad två & mer")

    assert link == (
        "mailto:jobb@exempel.se?subject=Ans%C3%B6kan%3A%20R%C3%B6r%20%26%20VVS"
        "&body=Hej%21%0ARad%20tv%C3%A5%20%26%20mer"
    )


@pytest.mark.parametrize("address", ["a@b.se, c@d.se", "a@b.se\nBcc: x@y.se", "", "Namn <a@b.se>"])
def test_mailto_link_requires_single_valid_address(address: str):
    assert mailto_link(address, "Ämne", "Text") is None


def test_mailto_link_encodes_url_characters_in_address():
    """Adresskontrollen godtar t.ex. `?` och `&`; de får inte lägga till fält i länken."""
    link = mailto_link("a?bcc=x@y.se", "Ämne", "Text")

    assert link is not None
    assert link.startswith("mailto:a%3Fbcc%3Dx@y.se?subject=")
    assert link.count("?") == 1


def test_filenames():
    from jobaplier.apply.package import cv_filename, letter_filename

    assert cv_filename("Vidar Mårtensson", "Åhléns & Co AB") == "CV_Vidar_Martensson_Ahlens_Co_AB.pdf"
    assert letter_filename("Åhléns & Co AB") == "Personligt_brev_Ahlens_Co_AB.pdf"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Åsa Öberg Ünal Élodie", "Asa_Oberg_Unal_Elodie"),
        ("Søren Ærø Straße Đorđe", "Soren_AEro_Strasse_Dorde"),
        ("  --A//B..  ", "A_B"),
        ("../../etc/passwd", "etc_passwd"),
        ("a\nb\tc\x00d", "a_b_c_d"),
        ("日本語", ""),
        ("", ""),
    ],
)
def test_safe_filename_part(text, expected):
    from jobaplier.apply.package import safe_filename_part

    assert safe_filename_part(text) == expected


def test_safe_filename_part_truncates_to_60_without_trailing_underscore():
    from jobaplier.apply.package import safe_filename_part

    assert safe_filename_part("a" * 100) == "a" * 60
    assert safe_filename_part("a" * 59 + " b") == "a" * 59
    assert len(safe_filename_part("ab " * 40)) <= 60


def test_filenames_with_empty_parts():
    from jobaplier.apply.package import cv_filename, letter_filename

    assert cv_filename("", "") == "CV_CV_Foretag.pdf"
    assert cv_filename("日本語", "!!!") == "CV_CV_Foretag.pdf"
    assert letter_filename("") == "Personligt_brev_Foretag.pdf"


# --- build_cv_pdf -----------------------------------------------------------------


OLD_DOC = CVDocument(
    name="Jane Doe",
    experience=[
        Experience(
            id="e1",
            title="Backendutvecklare",
            employer="Acme",
            period="2018 – 2024",
            bullets=["Byggde API:er i Python"],
        )
    ],
    skills=["Python"],
)

NEW_DOC = CVDocument(
    name="Jane Doe",
    experience=[
        Experience(
            id="e1",
            title="Teknisk chef",
            employer="Beta AB",
            period="2024 – nu",
            bullets=["Ledde tolv utvecklare"],
        ),
        Experience(
            id="e2",
            title="Backendutvecklare",
            employer="Acme",
            period="2018 – 2024",
            bullets=["Byggde API:er i Python"],
        ),
    ],
    skills=["Python", "Ledarskap"],
)

TAILORED = TailoredCV(
    language="sv",
    headline="Python-specialist",
    summary="Anpassad profiltext.",
    experience=[TailoredExperience(id="e1", bullets=["Skräddarsydd punkt om API:er"])],
    skills=["Python"],
    education_order=[],
)


def install_cv(paths: Paths, pdf: Path) -> None:
    shutil.copyfile(pdf, paths.cv)


def add_tailored_draft(engine, cv_hash: str) -> int:
    with session_scope(engine) as session:
        job = Job(dedupe_key="k", title="Utvecklare", company="Exempel AB")
        session.add(job)
        session.flush()
        draft = Draft(job_id=job.id, letter_text="Hej!")
        session.add(draft)
        session.flush()
        session.add(
            TailoredCVRecord(
                draft_id=draft.id,
                tailored_json=TAILORED.model_dump(mode="json"),
                warnings_json=[],
                cv_hash=cv_hash,
            )
        )
        return draft.id


def test_build_cv_pdf_tailored(engine, paths: Paths, config: Config, make_pdf, tmp_path: Path):
    install_cv(paths, make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."))
    draft_id = add_tailored_draft(engine, cv_file_hash(paths.cv))
    ai = FakeAI({CVDocument: [OLD_DOC]})
    out = tmp_path / "ut" / "cv.pdf"

    assert build_cv_pdf(engine, paths, config, ai, draft_id, out) == (out, True)
    text = extract_cv_text(out)
    assert "Skräddarsydd punkt om API:er" in text
    assert "Python-specialist" in text
    assert "Backendutvecklare" in text and "Acme" in text  # fakta från dokumentet
    assert "test@example.se" in text  # kontaktuppgift från inställningarna


def test_build_cv_pdf_falls_back_to_original(
    engine, paths: Paths, config: Config, make_pdf, tmp_path: Path
):
    install_cv(paths, make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."))
    ai = FakeAI({})
    out = tmp_path / "cv.pdf"
    assert build_cv_pdf(engine, paths, config, ai, 999, out) == (paths.cv, False)
    assert not out.exists()
    assert ai.calls == []


def test_build_cv_pdf_cv_missing(engine, paths: Paths, config: Config, tmp_path: Path):
    with pytest.raises(CVMissing):
        build_cv_pdf(engine, paths, config, FakeAI({}), 999, tmp_path / "cv.pdf")


def test_build_cv_pdf_after_cv_replaced(
    engine, paths: Paths, config: Config, make_pdf, tmp_path: Path
):
    """Review Focus 3: CV:t byttes efter utkastet. Det gamla anpassade CV:t används inte
    (dess id:n pekar på andra anställningar); i stället blir det nya CV:t i original."""
    install_cv(paths, make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."))
    draft_id = add_tailored_draft(engine, cv_file_hash(paths.cv))
    ai = FakeAI({CVDocument: [OLD_DOC, NEW_DOC]})
    build_cv_pdf(engine, paths, config, ai, draft_id, tmp_path / "forsta.pdf")

    install_cv(paths, make_pdf("Jane Doe. Teknisk chef på Beta AB 2024 – nu."))
    out = tmp_path / "cv.pdf"
    assert build_cv_pdf(engine, paths, config, ai, draft_id, out) == (out, False)
    assert [c["purpose"] for c in ai.calls] == ["cv_document", "cv_document"]  # byggdes om
    text = extract_cv_text(out)
    for expected in ("Teknisk chef", "Beta AB", "Ledde tolv utvecklare", "Acme", "Ledarskap"):
        assert expected in text
    assert "Skräddarsydd punkt" not in text
    assert "Python-specialist" not in text


DOC_A = CVDocument(
    name="Jane Doe",
    experience=[
        Experience(id="e1", title="Backendutvecklare", employer="Acme", period="2018 – 2024"),
        Experience(id="e2", title="Konsult", employer="Beta AB", period="2015 – 2018"),
    ],
)


def test_build_cv_pdf_after_swap_back_keeps_bullets_with_right_employer(
    engine, paths: Paths, config: Config, make_pdf, tmp_path: Path
):
    """CV A -> B -> A: anpassningen gjordes mot A:s dokument (e1 = Acme). Struktureras A
    om i en annan ordning hamnar punkterna under fel arbetsgivare; dokumentet för A ska
    därför återanvändas."""
    cv_a = make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024. Konsult på Beta AB.")
    install_cv(paths, cv_a)
    draft_id = add_tailored_draft(engine, cv_file_hash(paths.cv))
    reordered = DOC_A.model_copy(update={"experience": list(reversed(DOC_A.experience))})
    ai = FakeAI({CVDocument: [DOC_A, NEW_DOC, reordered]})
    build_cv_pdf(engine, paths, config, ai, draft_id, tmp_path / "a.pdf")

    install_cv(paths, make_pdf("Jane Doe. Teknisk chef på Beta AB 2024 – nu."))
    build_cv_pdf(engine, paths, config, ai, draft_id, tmp_path / "b.pdf")

    install_cv(paths, cv_a)
    out = tmp_path / "a-igen.pdf"
    assert build_cv_pdf(engine, paths, config, ai, draft_id, out) == (out, True)
    assert len(ai.calls) == 2  # A struktureras bara en gång
    text = " ".join(extract_cv_text(out).split())
    assert text.index("Acme") < text.index("Skräddarsydd punkt om API:er") < text.index("Beta AB")


def test_build_cv_pdf_falls_back_when_structuring_fails(
    engine, paths: Paths, config: Config, make_pdf, tmp_path: Path, caplog
):
    install_cv(paths, make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."))
    draft_id = add_tailored_draft(engine, cv_file_hash(paths.cv))
    ai = FakeAI({CVDocument: [AIError("Claude svarar inte")]})
    out = tmp_path / "cv.pdf"

    assert build_cv_pdf(engine, paths, config, ai, draft_id, out) == (paths.cv, False)
    assert "Claude svarar inte" in caplog.text


def test_build_cv_pdf_falls_back_when_rendering_fails(
    engine, paths: Paths, config: Config, make_pdf, tmp_path: Path, monkeypatch, caplog
):
    install_cv(paths, make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."))
    draft_id = add_tailored_draft(engine, cv_file_hash(paths.cv))

    def broken(cv, out):
        raise OSError("WeasyPrint kraschade")

    monkeypatch.setattr(package_module, "render_cv_pdf", broken)
    ai = FakeAI({CVDocument: [OLD_DOC]})

    assert build_cv_pdf(engine, paths, config, ai, draft_id, tmp_path / "cv.pdf") == (
        paths.cv,
        False,
    )
    assert "WeasyPrint kraschade" in caplog.text


def test_build_cv_pdf_cv_missing_with_tailored_record_still_raises(
    engine, paths: Paths, config: Config, tmp_path: Path
):
    draft_id = add_tailored_draft(engine, "gammal-hash")
    with pytest.raises(CVMissing):
        build_cv_pdf(engine, paths, config, FakeAI({}), draft_id, tmp_path / "cv.pdf")
