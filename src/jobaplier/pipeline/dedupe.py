"""Dubblettnyckel och upsert av insamlade annonser."""

import re
import unicodedata

from sqlmodel import Session, select

from jobaplier.models import ApplyMethod, Job, JobSource, JobStatus
from jobaplier.sources.base import RawJob

_COMPANY_FORM_TOKENS = frozenset(
    {
        "ab", "aktiebolag", "publ", "ltd", "limited", "inc", "gmbh",
        "as", "asa", "oy", "oyj", "aps", "llc",
    }
)  # fmt: skip
_NON_ALNUM = re.compile(r"[\W_]+")

# Högre är bättre: mejl > formulär > okänt.
_APPLY_RANK = {ApplyMethod.unknown: 0, ApplyMethod.form: 1, ApplyMethod.email: 2}
# Bara jobb som ännu inte är i ansökningsflödet får ny ansökningsväg. Efter det har
# användaren sett (och kanske godkänt) en viss kanal, som inte får bytas i det tysta.
_UPGRADABLE = frozenset({JobStatus.new, JobStatus.filtered, JobStatus.scored})


def normalize(text: str) -> str:
    """Casefold, allt utom bokstäver/siffror blir mellanslag. å/ä/ö behålls."""
    folded = unicodedata.normalize("NFC", text).casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


def normalize_company(company: str) -> str:
    tokens = normalize(company).split()
    kept = [t for t in tokens if t not in _COMPANY_FORM_TOKENS]
    # Ett namn som bara består av en bolagsform ("AB") behåller sin text.
    return " ".join(kept or tokens)


def dedupe_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize(title)}"


def _upgrade_apply(job: Job, raw: RawJob) -> None:
    """Uppgradera ansökningsväg bara om den nya är strikt bättre och jobbet ännu inte
    har lämnat poängsättningen."""
    if job.status not in _UPGRADABLE:
        return
    if _APPLY_RANK[raw.apply.method] <= _APPLY_RANK[job.apply_method]:
        return
    job.apply_method = raw.apply.method
    if raw.apply.method == ApplyMethod.email:
        job.apply_email = raw.apply.email
        job.apply_reference = raw.apply.reference
    elif raw.apply.method == ApplyMethod.form:
        job.apply_form_url = raw.apply.form_url


def _attach_source(session: Session, job: Job, raw: RawJob) -> None:
    session.add(
        JobSource(
            job_id=job.id,  # type: ignore[arg-type]
            source=raw.source,
            external_id=raw.external_id,
            url=raw.url,
            raw_json=None,
        )
    )


def upsert_raw_jobs(session: Session, raws: list[RawJob]) -> list[Job]:
    """Lägg in annonser; returnerar bara nya jobb. Gör flush men inte commit."""
    created: list[Job] = []
    for raw in raws:
        known = session.exec(
            select(JobSource.id).where(
                JobSource.source == raw.source,
                JobSource.external_id == raw.external_id,
            )
        ).first()
        if known is not None:
            continue

        key = dedupe_key(raw.company, raw.title)
        job = session.exec(select(Job).where(Job.dedupe_key == key)).first()
        if job is not None:
            _upgrade_apply(job, raw)
            _attach_source(session, job, raw)
            session.flush()
            continue

        job = Job(
            dedupe_key=key,
            title=raw.title,
            company=raw.company,
            location=raw.location,
            remote=raw.remote,
            description=raw.description,
            url=raw.url,
            published_at=raw.published_at,
            apply_method=raw.apply.method,
            apply_email=raw.apply.email,
            apply_form_url=raw.apply.form_url,
            apply_reference=raw.apply.reference,
            incomplete=raw.incomplete,
        )
        session.add(job)
        session.flush()
        _attach_source(session, job, raw)
        session.flush()
        created.append(job)
    return created
