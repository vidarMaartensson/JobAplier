"""Ansökningspaketet: mejlets ämne, brödtext och adresskontroll, filnamn och CV-PDF."""

import logging
import re
import unicodedata
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine
from sqlmodel import Session, select

from jobaplier.ai import AIClient
from jobaplier.apply.cv_pdf import render_cv_pdf
from jobaplier.apply.tailor import TailoredCV, original_cv, verify_tailored, with_contact_fallback
from jobaplier.config import Config, Paths
from jobaplier.db import session_scope
from jobaplier.models import Draft, Job, TailoredCVRecord
from jobaplier.profile.cv import CVMissing, cv_file_hash
from jobaplier.profile.cv_document import load_or_build_cv_document

log = logging.getLogger(__name__)

BODY_SV = (
    "Hej!\n\nHär kommer min ansökan till tjänsten {title}. "
    "CV och personligt brev bifogas.\n\nVänliga hälsningar\n{name}\n{phone}"
)
BODY_EN = (
    "Hello,\n\nPlease find attached my application for the {title} position, "
    "including my CV and cover letter.\n\nBest regards\n{name}\n{phone}"
)

_ADDRESS_RE = re.compile(r"[^\s,;<>@]+@[^\s,;<>@]+\.[^\s,;<>@]+")


def valid_single_address(address: str) -> bool:
    """Exakt en ren adress: inga radbrytningar, listor eller visningsnamn."""
    return parseaddr(address)[1] == address and _ADDRESS_RE.fullmatch(address) is not None


def _one_line(text: str) -> str:
    """Rubriker får inte innehålla radbrytningar (annonstexter kan göra det)."""
    return " ".join(text.split())


def application_subject(job: Job) -> str:
    subject = f"Ansökan: {_one_line(job.title)}"
    if job.apply_reference:
        subject += f" (ref. {_one_line(job.apply_reference)})"
    return subject


def email_body(job: Job, draft: Draft, config: Config) -> str:
    template = BODY_EN if draft.language == "en" else BODY_SV
    return template.format(
        title=_one_line(job.title), name=config.profile.name, phone=config.profile.phone
    )


def mailto_link(to: str, subject: str, body: str) -> str | None:
    """`mailto:`-länk utan bilagor, eller `None` om `to` inte är exakt en giltig adress.

    Adresskontrollen godtar tecken som `?`, `&` och `#`; de kodas också, så att en adress
    från annonsen aldrig kan lägga till fält (t.ex. `bcc`) i länken."""
    if not valid_single_address(to):
        return None
    return (
        f"mailto:{quote(to, safe='@+')}"
        f"?subject={quote(subject, safe='')}&body={quote(body, safe='')}"
    )


MAX_FILENAME_PART = 60

# Tecken som NFKD inte delar upp i bokstav + accent.
_TRANSLITERATION = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ß": "ss",
        "đ": "d",
        "Đ": "D",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
        "ł": "l",
        "Ł": "L",
        "ı": "i",
    }
)


def safe_filename_part(text: str) -> str:
    """ASCII-säker del av ett filnamn: translittererad, `[A-Za-z0-9]` och `_`, högst 60 tecken."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return cleaned[:MAX_FILENAME_PART].rstrip("_")


def cv_filename(name: str, company: str) -> str:
    return f"CV_{safe_filename_part(name) or 'CV'}_{safe_filename_part(company) or 'Foretag'}.pdf"


def letter_filename(company: str) -> str:
    return f"Personligt_brev_{safe_filename_part(company) or 'Foretag'}.pdf"


def tailored_record(session: Session, draft_id: int) -> TailoredCVRecord | None:
    return session.exec(
        select(TailoredCVRecord).where(TailoredCVRecord.draft_id == draft_id)
    ).first()


def build_cv_pdf(
    engine: Engine, paths: Paths, config: Config, ai: AIClient, draft_id: int, out: Path
) -> tuple[Path, bool]:
    """CV:t för utkastet. `(out, True)` när det skräddarsydda CV:t renderades till `out`.

    Utan skräddarsytt CV returneras `(paths.cv, False)` och inget skrivs; anroparen
    använder originalfilen. Har CV-filen bytts sedan anpassningen renderas det nya
    CV:t utan anpassning till `out`, med `(out, False)`. `ai` används bara om
    CV-dokumentet måste byggas om. Misslyckas struktureringen eller renderingen loggas
    felet och originalfilen används, `(paths.cv, False)`. Saknas CV-filen kastas
    `CVMissing`.
    """
    cv_hash = cv_file_hash(paths.cv)
    with session_scope(engine) as session:
        record = tailored_record(session, draft_id)
        if record is None:
            return paths.cv, False
        tailored_json = dict(record.tailored_json)
        replaced = record.cv_hash != cv_hash
    try:
        tailored = TailoredCV.model_validate(tailored_json)
        with session_scope(engine) as session:
            doc = load_or_build_cv_document(session, paths.cv, ai)
        # Anpassningens id:n hör till det gamla CV:t och kan peka på andra anställningar.
        cv = original_cv(doc) if replaced else verify_tailored(tailored, doc)[0]
        render_cv_pdf(with_contact_fallback(cv, config.profile), out)
    except CVMissing:
        raise
    except Exception:
        log.exception("CV:t för utkast %s kunde inte skapas; originalet används", draft_id)
        return paths.cv, False
    return out, not replaced
