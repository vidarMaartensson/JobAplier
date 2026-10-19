import hashlib
import shutil
from pathlib import Path

import pytest
from fakes import FakeAI
from sqlmodel import select

from jobaplier.ai import WRITING_MODEL
from jobaplier.db import session_scope
from jobaplier.models import ProfileRecord
from jobaplier.profile.cv import (
    CVMissing,
    CVUnreadable,
    Profile,
    extract_cv_text,
    load_or_build_profile,
    profile_of,
)

CV_TEXT = "Python-utvecklare med 5 års erfarenhet"


def make_profile(summary: str = "Backendutvecklare") -> Profile:
    return Profile(
        summary=summary,
        skills=["Python", "SQL"],
        years_experience=5.0,
        roles=["Utvecklare"],
        languages=["Svenska", "Engelska"],
        education=["civilingenjör"],
    )


def install_cv(paths, pdf: Path) -> None:
    shutil.copyfile(pdf, paths.cv)


def test_extract_text(make_pdf):
    assert "Python-utvecklare" in extract_cv_text(make_pdf(CV_TEXT))


def test_builds_profile_on_first_run(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({Profile: [make_profile()]})
    with session_scope(engine) as session:
        record = load_or_build_profile(session, paths.cv, ai)
        assert record.cv_hash == hashlib.sha256(paths.cv.read_bytes()).hexdigest()
        assert "Python-utvecklare" in record.cv_text
        assert profile_of(record) == make_profile()
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["model"] == WRITING_MODEL == "claude-sonnet-5-5"
    assert call["purpose"] == "profile"
    assert call["schema"] is Profile
    assert "Python-utvecklare" in call["prompt"]
    with session_scope(engine) as session:
        assert len(session.exec(select(ProfileRecord)).all()) == 1


def test_reuses_profile_when_cv_unchanged(engine, paths, make_pdf):
    install_cv(paths, make_pdf(CV_TEXT))
    ai = FakeAI({Profile: [make_profile()]})
    with session_scope(engine) as session:
        first_id = load_or_build_profile(session, paths.cv, ai).id
    with session_scope(engine) as session:
        second = load_or_build_profile(session, paths.cv, ai)
        assert second.id == first_id
    assert len(ai.calls) == 1


def test_rebuilds_when_cv_changed(engine, paths, make_pdf):
    ai = FakeAI({Profile: [make_profile("A"), make_profile("B")]})
    install_cv(paths, make_pdf(CV_TEXT))
    with session_scope(engine) as session:
        first = load_or_build_profile(session, paths.cv, ai)
        first_id = first.id
    install_cv(paths, make_pdf("Projektledare med 10 års erfarenhet"))
    with session_scope(engine) as session:
        second = load_or_build_profile(session, paths.cv, ai)
        assert second.id != first_id
        assert profile_of(second).summary == "B"
    assert len(ai.calls) == 2
    with session_scope(engine) as session:
        assert len(session.exec(select(ProfileRecord)).all()) == 2


def test_missing_cv_raises(engine, paths):
    ai = FakeAI({Profile: []})
    with session_scope(engine) as session, pytest.raises(CVMissing) as exc:
        load_or_build_profile(session, paths.cv, ai)
    assert str(exc.value)
    assert ai.calls == []


def test_empty_text_raises(engine, paths, make_pdf):
    install_cv(paths, make_pdf(""))
    ai = FakeAI({Profile: []})
    with session_scope(engine) as session, pytest.raises(CVUnreadable):
        load_or_build_profile(session, paths.cv, ai)
    assert ai.calls == []


def test_corrupt_pdf_raises_unreadable(paths):
    paths.cv.write_bytes(b"not a pdf")
    with pytest.raises(CVUnreadable):
        extract_cv_text(paths.cv)
