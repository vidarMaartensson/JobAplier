"""Adapter för ReachMee (web*.reachmee.com)."""

from __future__ import annotations

import re

from playwright.sync_api import Page

from jobaplier.apply.form.ats import AtsAdapter, KnownField
from jobaplier.apply.form.fields import Applicant

# Formulär som postar till reachmee.com känns igen även på kundens egen domän.
# OVERIFIERAD signatur och OVERIFIERADE selektorer: ansökningsformuläret ligger bakom
# konto/inloggning och kunde inte hämtas, så markupen är skriven efter minnet.
_SIGNATURE = re.compile(r"""<form[^>]*\baction=["'][^"']*reachmee\.com""", re.IGNORECASE)

_FIRST = 'input[name*="first_name" i], input[name*="firstname" i]'
_LAST = 'input[name*="last_name" i], input[name*="lastname" i]'
_EMAIL = 'input[name*="email" i]'
_PHONE = 'input[name*="mobile" i], input[name*="phone" i]'
_CV = 'input[type="file"][name*="cv" i], input[type="file"][id*="cv" i]'
_LETTER = (
    'input[type="file"][name*="letter" i], input[type="file"][id*="letter" i], '
    'input[type="file"][name*="brev" i], input[type="file"][id*="brev" i]'
)


class ReachMeeAdapter(AtsAdapter):
    name = "reachmee"
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
