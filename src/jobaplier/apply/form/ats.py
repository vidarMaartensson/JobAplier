"""Gemensam bas för adaptrar till kända rekryteringssystem (ATS).

En ATS-adapter fyller först systemets kända fält med fasta selektorer och låter sedan
GenericAdapter ta hand om resten. Allt sker inom samma submit-skydd som
GenericAdapter använder. Adaptrarna fyller bara i fält: de klickar aldrig (inte ens på
den stylade knapp som ersätter ett dolt filfält), trycker aldrig Enter, skickar aldrig
formuläret och rör aldrig samtyckes-/villkorsrutor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from jobaplier.apply.form.fields import Applicant
from jobaplier.apply.form.generic import (
    FILL_TIMEOUT_MS,
    FillReport,
    GenericAdapter,
    submit_guard,
)
from jobaplier.sources.ats import detect_ats


@dataclass
class KnownField:
    """Ett fält som adaptern känner till.

    `locator=None` betyder att fältet finns i formuläret men inte kan pekas ut säkert;
    det lämnas orört och rapporteras som obesvarat."""

    label: str
    locator: Locator | None
    value: str | Path
    file: bool = False


def merge_reports(*reports: FillReport) -> FillReport:
    """Slår ihop rapporter i ordning och tar bort dubbletter av etiketter."""
    filled: list[str] = []
    unanswered: list[str] = []
    for report in reports:
        filled.extend(label for label in report.filled if label not in filled)
        unanswered.extend(label for label in report.unanswered if label not in unanswered)
    return FillReport(filled=filled, unanswered=unanswered)


class AtsAdapter:
    """Basklass. Underklasser sätter `name` och `html_signature` och implementerar
    `known_fields`."""

    name: str
    html_signature: re.Pattern[str]

    def matches(self, url: str, html: str) -> bool:
        return detect_ats(url) == self.name or self.html_signature.search(html) is not None

    def known_fields(self, page: Page, applicant: Applicant) -> list[KnownField]:
        raise NotImplementedError

    def fill(self, page: Page, applicant: Applicant) -> FillReport:
        with submit_guard(page):
            known = self._fill_known(page, applicant)
            generic = GenericAdapter().fill(page, applicant)
        return merge_reports(known, generic)

    def _fill_known(self, page: Page, applicant: Applicant) -> FillReport:
        report = FillReport()
        for known in self.known_fields(page, applicant):
            if not known.value:
                continue
            if known.locator is None:
                report.unanswered.append(known.label)
                continue
            try:
                done = _fill_one(known)
            except PlaywrightError:
                report.unanswered.append(known.label)  # finns men gick inte att fylla
                continue
            if done:
                report.filled.append(known.label)
        return report


def _fill_one(known: KnownField) -> bool:
    """True om fältet fylldes. False om det saknas eller redan är ifyllt/otillgängligt."""
    assert known.locator is not None
    if known.locator.count() == 0:
        return False
    locator = known.locator.first
    if known.file:
        # Filfält är ofta dolda bakom en stylad knapp; set_input_files kräver inte synlighet.
        if locator.evaluate("el => el.files.length") > 0:
            return False
        locator.set_input_files(known.value, timeout=FILL_TIMEOUT_MS)
        return True
    if not locator.is_visible() or not locator.is_editable() or locator.input_value():
        return False
    locator.fill(str(known.value), timeout=FILL_TIMEOUT_MS)
    return True
