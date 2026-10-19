"""Adapter för Varbi (<kund>.varbi.com och profile.varbi.com)."""

from __future__ import annotations

import re

from playwright.sync_api import Page

from jobaplier.apply.form.ats import AtsAdapter, KnownField
from jobaplier.apply.form.fields import Applicant

# Varbi-sidor på egen domän känns igen på CSRF-metataggen: <meta name="csrf-param"
# content="_varbi_csrf_token_…">. Verifierad mot en riktig publik Varbi-annons; att
# den även finns på ansökningssidan är antaget (den ligger bakom inloggning).
_SIGNATURE = re.compile(
    r"""name=["']csrf-param["'][^>]*content=["']_varbi_csrf_token"""
    r"""|content=["']_varbi_csrf_token[^>]*name=["']csrf-param["']"""
)

# OVERIFIERADE selektorer (ansökningsformuläret kunde inte hämtas utan inloggning):
# substrängsmatchning på name/id för att tåla små variationer mellan kunder.
_FIRST = 'input[name*="firstname" i], input[name*="first_name" i]'
_LAST = 'input[name*="lastname" i], input[name*="last_name" i]'
_EMAIL = 'input[type="email"], input[name*="email" i]'
_PHONE = 'input[type="tel"], input[name*="phone" i], input[name*="mobile" i]'
_CV = 'input[type="file"][name*="cv" i], input[type="file"][id*="cv" i]'
_LETTER = (
    'input[type="file"][name*="letter" i], input[type="file"][id*="letter" i], '
    'input[type="file"][name*="brev" i], input[type="file"][id*="brev" i]'
)


class VarbiAdapter(AtsAdapter):
    name = "varbi"
    html_signature = _SIGNATURE

    def known_fields(self, page: Page, applicant: Applicant) -> list[KnownField]:
        return [
            KnownField("Förnamn", page.locator(_FIRST), applicant.first_name),
            KnownField("Efternamn", page.locator(_LAST), applicant.last_name),
            KnownField("E-post", page.locator(_EMAIL), applicant.email),
            KnownField("Telefon", page.locator(_PHONE), applicant.phone),
            KnownField("CV", page.locator(_CV), applicant.cv_path, file=True),
            KnownField("Personligt brev", page.locator(_LETTER), applicant.letter_pdf, file=True),
        ]
