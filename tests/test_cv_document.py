import shutil
from pathlib import Path

import pytest
from fakes import FakeAI
from sqlmodel import select

from jobaplier.ai import WRITING_MODEL
from jobaplier.db import session_scope
from jobaplier.models import CVDocumentRecord
from jobaplier.profile.cv import CVMissing, cv_file_hash
from jobaplier.profile.cv_document import (
    CVDocument,
    Education,
    Experience,
    OtherSection,
    cached_cv_document,
    cv_document_warnings,
    fidelity_warnings,
    load_or_build_cv_document,
    renumber,
)

CV_TEXT = "Backendutvecklare på Acme 2021 – nu"


def make_doc(name: str = "Test Testsson") -> CVDocument:
    return CVDocument(
        name=name,
        experience=[
            Experience(id="x", title="Backendutvecklare", employer="Acme", period="2021 – nu"),
            Experience(id="y", title="Konsult", employer="Beta AB", period="2018 – 2021"),
        ],
        education=[Education(id="q", degree="Civilingenjör", school="KTH", period="2013 – 2018")],
    )


def install_cv(paths, pdf: Path) -> None:
    shutil.copyfile(pdf, paths.cv)


def test_defaults():
    doc = CVDocument()
    assert doc.language == "sv"
    assert doc.name == doc.email == doc.summary == ""
    assert doc.experience == doc.education == doc.skills == doc.languages == doc.other == []
    assert Experience().bullets == []
    assert OtherSection().items == []


def test_renumber_returns_new_object():
    doc = make_doc()
    out = renumber(doc)
    assert [e.id for e in out.experience] == ["e1", "e2"]
    assert [e.id for e in out.education] == ["u1"]
    assert [e.id for e in doc.experience] == ["x", "y"]
    assert out is not doc


def test_builds_and_renumbers(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({CVDocument: [make_doc()]})
    with session_scope(engine) as session:
        doc = load_or_build_cv_document(session, paths.cv, ai)
        assert [e.id for e in doc.experience] == ["e1", "e2"]
        assert [e.id for e in doc.education] == ["u1"]
        assert doc.experience[0].period == "2021 – nu"
        rows = session.exec(select(CVDocumentRecord)).all()
        assert len(rows) == 1
        assert rows[0].document_json["experience"][1]["id"] == "e2"
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["purpose"] == "cv_document"
    assert call["model"] == WRITING_MODEL
    assert call["schema"] is CVDocument
    assert "Backendutvecklare på Acme" in call["prompt"]
    assert "verbatim" in call["system"]


def test_reuses_when_hash_unchanged(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({CVDocument: [make_doc()]})
    with session_scope(engine) as session:
        first = load_or_build_cv_document(session, paths.cv, ai)
    with session_scope(engine) as session:
        second = load_or_build_cv_document(session, paths.cv, ai)
        assert len(session.exec(select(CVDocumentRecord)).all()) == 1
    assert len(ai.calls) == 1
    assert second == first


def test_rebuilds_when_cv_changes(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({CVDocument: [make_doc("Först"), make_doc("Sedan")]})
    with session_scope(engine) as session:
        assert load_or_build_cv_document(session, paths.cv, ai).name == "Först"
    install_cv(paths, make_pdf("Helt annat CV"))
    with session_scope(engine) as session:
        assert load_or_build_cv_document(session, paths.cv, ai).name == "Sedan"
        assert len(session.exec(select(CVDocumentRecord)).all()) == 2
    # tillbaka till senaste raden: samma hash som rad 2 -> inget nytt anrop
    with session_scope(engine) as session:
        assert load_or_build_cv_document(session, paths.cv, ai).name == "Sedan"
    assert len(ai.calls) == 2


def test_swap_back_reuses_first_document(engine, paths, make_pdf):
    """CV A -> B -> A: dokumentet för A återanvänds (samma id:n), inget nytt anrop."""
    cv_a = make_pdf(CV_TEXT)
    install_cv(paths, cv_a)
    reordered = make_doc("A igen")
    reordered.experience.reverse()
    ai = FakeAI({CVDocument: [make_doc("A"), make_doc("B"), reordered]})
    with session_scope(engine) as session:
        first = load_or_build_cv_document(session, paths.cv, ai)
    install_cv(paths, make_pdf("Helt annat CV"))
    with session_scope(engine) as session:
        assert load_or_build_cv_document(session, paths.cv, ai).name == "B"
    install_cv(paths, cv_a)
    with session_scope(engine) as session:
        again = load_or_build_cv_document(session, paths.cv, ai)
        assert len(session.exec(select(CVDocumentRecord)).all()) == 2
    assert again == first
    assert again.experience[0].employer == "Acme"
    assert len(ai.calls) == 2


def test_missing_cv_raises(engine, paths):
    ai = FakeAI({CVDocument: []})
    with session_scope(engine) as session:
        with pytest.raises(CVMissing):
            load_or_build_cv_document(session, paths.cv, ai)
    assert ai.calls == []


# --- bara cachen (/cv) ---------------------------------------------------------------


def add_record(engine, cv_hash: str, doc: CVDocument) -> None:
    with session_scope(engine) as session:
        session.add(CVDocumentRecord(cv_hash=cv_hash, document_json=doc.model_dump(mode="json")))


def test_cached_returns_latest_row_for_current_cv(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    current = cv_file_hash(paths.cv)
    add_record(engine, current, make_doc("Äldre"))
    add_record(engine, current, make_doc("Senaste"))
    add_record(engine, "annat-cv", make_doc("Annat CV"))

    with session_scope(engine) as session:
        doc = cached_cv_document(session, paths.cv)

    assert doc is not None
    assert doc.name == "Senaste"
    assert doc.experience[0].title == "Backendutvecklare"


def test_cached_is_none_without_matching_row(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    with session_scope(engine) as session:
        assert cached_cv_document(session, paths.cv) is None
    add_record(engine, "annat-cv", make_doc())
    with session_scope(engine) as session:
        assert cached_cv_document(session, paths.cv) is None
        assert len(session.exec(select(CVDocumentRecord)).all()) == 1  # inget byggs


def test_cached_missing_cv_raises(engine, paths):
    with session_scope(engine) as session:
        with pytest.raises(CVMissing):
            cached_cv_document(session, paths.cv)


# --- kontroll av faktafälten mot CV-texten ----------------------------------------------


def warning(value: str) -> str:
    return f'Kontrollera i ditt CV: "{value}" hittades inte ordagrant i cv.pdf'


def test_fidelity_accepts_verbatim_facts_with_whitespace_case_and_dash_variants():
    doc = CVDocument(
        experience=[
            Experience(
                title="Senior utvecklare – Stockholm",
                employer="Acme AB",
                period="2021 — nu",
            )
        ],
        education=[Education(degree="Civilingenjör, datateknik", school="KTH")],
    )
    text = "SENIOR utvecklare - Stockholm\nacme   ab\n2021–nu\nCivilingenjör,\ndatateknik KTH"
    assert fidelity_warnings(doc, text) == []


def test_fidelity_warns_for_each_missing_fact_once():
    doc = CVDocument(
        experience=[
            Experience(title="Backendutvecklare", employer="Acme", period="2021 – nu"),
            Experience(title="Konsult i Göteborg", employer="Acme", period="2018 – 2021"),
            Experience(title="", employer="Gamma", period=""),
        ],
        education=[Education(degree="Civilingenjör", school="Chalmers", period="2013")],
    )
    text = "Backendutvecklare Acme 2021 – nu Konsult Göteborg 2018 – 2021 Civilingenjör KTH"
    assert fidelity_warnings(doc, text) == [
        warning("Konsult i Göteborg"),
        warning("Gamma"),
        warning("Chalmers"),
    ]


def test_build_stores_fidelity_warnings(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({CVDocument: [make_doc()]})
    with session_scope(engine) as session:
        assert cv_document_warnings(session, paths.cv) == []  # inget byggt än
        load_or_build_cv_document(session, paths.cv, ai)
    expected = [warning(v) for v in ("Konsult", "Beta AB", "2018 – 2021", "Civilingenjör", "KTH")]
    with session_scope(engine) as session:
        assert cv_document_warnings(session, paths.cv) == expected
        load_or_build_cv_document(session, paths.cv, ai)  # återanvänds
        assert cv_document_warnings(session, paths.cv) == expected
    install_cv(paths, make_pdf("Helt annat CV"))
    with session_scope(engine) as session:
        assert cv_document_warnings(session, paths.cv) == []  # gäller bara den filen
    assert len(ai.calls) == 1


def test_warnings_empty_for_row_built_before_the_check(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    add_record(engine, cv_file_hash(paths.cv), make_doc())
    with session_scope(engine) as session:
        assert cv_document_warnings(session, paths.cv) == []


def test_prompt_keeps_titles_whole():
    from jobaplier.profile.cv_document import CV_DOCUMENT_SYSTEM_PROMPT as prompt

    assert "Copy each `title` exactly as it appears in the CV, as one line" in prompt
    assert "place names" in prompt and "dashes" in prompt
    assert "Fill `location` only when the CV gives it as a separate field" in prompt
