"""CV-textextraktion och cachad AI-byggd profil."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlmodel import Session, select

from jobaplier.ai import WRITING_MODEL, AIClient
from jobaplier.models import ProfileRecord

PROFILE_SYSTEM_PROMPT = (
    "Du läser ett CV och sammanfattar det som en strukturerad profil. "
    "Extrahera endast det som uttryckligen står i CV:t; hitta inte på något. "
    "Om en uppgift saknas ska motsvarande lista vara tom och years_experience vara 0. "
    "Skriv sammanfattningen på samma språk som CV:t."
)


class Profile(BaseModel):
    summary: str
    skills: list[str]
    years_experience: float
    roles: list[str]
    languages: list[str]
    education: list[str]


class CVMissing(Exception):
    """CV-filen finns inte. Meddelandet är på svenska."""


class CVUnreadable(Exception):
    """CV-filen går inte att läsa eller saknar text. Meddelandet är på svenska."""


def extract_cv_text(pdf: Path) -> str:
    try:
        reader = PdfReader(pdf)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except FileNotFoundError:
        raise CVMissing(f"CV-filen saknas: {pdf}") from None
    except (PyPdfError, OSError, ValueError) as exc:
        raise CVUnreadable(f"CV-filen {pdf.name} kunde inte läsas som PDF: {exc}") from exc
    text = text.strip()
    if not text:
        raise CVUnreadable(
            f"CV-filen {pdf.name} innehåller ingen läsbar text "
            "(är det en inskannad bild? Använd en PDF med markerbar text)"
        )
    return text


def cv_file_hash(cv_path: Path) -> str:
    """sha256 av CV-filens innehåll. Cache-nyckel för profil och strukturerat CV."""
    try:
        return hashlib.sha256(cv_path.read_bytes()).hexdigest()
    except FileNotFoundError:
        raise CVMissing(f"CV-filen saknas: {cv_path}") from None
    except OSError as exc:
        raise CVUnreadable(f"CV-filen {cv_path.name} kunde inte läsas: {exc}") from exc


def load_or_build_profile(session: Session, cv_path: Path, ai: AIClient) -> ProfileRecord:
    """Returnerar senaste profilen om CV:t är oförändrat, annars byggs en ny.

    Den nya raden läggs till och flushas; anroparen committar sessionen.
    """
    cv_hash = cv_file_hash(cv_path)

    latest = session.exec(select(ProfileRecord).order_by(ProfileRecord.id.desc())).first()
    if latest is not None and latest.cv_hash == cv_hash:
        return latest

    cv_text = extract_cv_text(cv_path)
    result = ai.complete(
        model=WRITING_MODEL,
        system=PROFILE_SYSTEM_PROMPT,
        prompt=f"CV:\n\n{cv_text}",
        schema=Profile,
        purpose="profile",
        effort="medium",
    )
    record = ProfileRecord(
        cv_hash=cv_hash, cv_text=cv_text, profile_json=result.value.model_dump(mode="json")
    )
    session.add(record)
    session.flush()
    return record


def profile_of(record: ProfileRecord) -> Profile:
    return Profile.model_validate(record.profile_json)
