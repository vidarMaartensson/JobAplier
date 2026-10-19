"""Hårt filter mot användarens preferenser (ingen AI)."""

import re

from jobaplier.config import Preferences
from jobaplier.models import Job
from jobaplier.pipeline.dedupe import normalize_company

_SENIORITY_MARKERS: dict[str, list[str]] = {
    "junior": ["junior"],
    "senior": ["senior", "sr."],
    "lead": ["lead", "principal", "staff", "head of"],
}  # nivån "mid" har inga markörer


def _has_marker(title: str, marker: str) -> bool:
    # Hel ord/fras; lookarounds i stället för \b så att "sr." fungerar.
    return re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", title) is not None


def hard_filter(job: Job, prefs: Preferences) -> str | None:
    """Returnerar filter_reason (på svenska) eller None om jobbet släpps igenom."""
    company = normalize_company(job.company)
    if company in {normalize_company(c) for c in prefs.exclude_companies}:
        return f"Uteslutet företag: {job.company}"

    title = job.title.casefold()
    for kw in prefs.exclude_keywords:
        if kw.casefold() in title:
            return f"Uteslutet nyckelord: {kw}"

    location = job.location
    if (
        prefs.locations
        and location is not None
        and location.strip()  # tom ort räknas som okänd
        and not any(loc.casefold() in location.casefold() for loc in prefs.locations)
        and not (prefs.remote_ok and job.remote is True)
    ):
        return f"Ort matchar inte: {location}"

    if prefs.seniority:
        allowed = {s.casefold() for s in prefs.seniority}
        for level, markers in _SENIORITY_MARKERS.items():
            if level in allowed:
                continue
            if any(_has_marker(title, m) for m in markers):
                return f"Senioritet: {level}"

    return None
