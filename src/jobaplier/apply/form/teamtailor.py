"""Adapter för Teamtailor (karriärsidor på *.teamtailor.com eller företagets egen domän)."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from jobaplier.apply.form.ats import AtsAdapter, KnownField
from jobaplier.apply.form.fields import Applicant

# Teamtailor-sajter ligger ofta på egen domän, så utöver detect_ats() känns systemet
# igen på formulärets fältnamn (candidate[first_name]); det är stabilt i Teamtailors
# Rails-formulär och verifierat mot en riktig ansökningssida.
_SIGNATURE = re.compile(r"""name=["']candidate\[first_name\]["']""")

# Teamtailor bygger filväljarna med Dropzone i webbläsaren: varje uppladdningsblock
# (#upload_<typ>_field) får en dold input.dz-hidden-input sist i <body>, i samma
# ordning som blocken. Blocken har själva ingen <input type=file>.
_UPLOAD_BLOCKS_JS = """
() => [...document.querySelectorAll('[id^="upload_"][id$="_field"]')].map((e) => e.id)
"""
_DROPZONE_INPUTS = 'input.dz-hidden-input[type="file"]'
_CV_BLOCK = "upload_resume_field"
_LETTER_BLOCK = "upload_cover_letter_field"


class TeamtailorAdapter(AtsAdapter):
    name = "teamtailor"
    html_signature = _SIGNATURE

    def known_fields(self, page: Page, applicant: Applicant) -> list[KnownField]:
        fields = [
            KnownField("Förnamn", page.locator("#candidate_first_name"), applicant.first_name),
            KnownField("Efternamn", page.locator("#candidate_last_name"), applicant.last_name),
            KnownField("E-post", page.locator("#candidate_email"), applicant.email),
            KnownField("Telefon", page.locator("#candidate_phone"), applicant.phone),
        ]
        uploads = self._upload_inputs(page)
        if _CV_BLOCK in uploads:
            fields.append(self._file_field(page, uploads, _CV_BLOCK, "CV", applicant.cv_path))
        if _LETTER_BLOCK in uploads:
            fields.append(
                self._file_field(
                    page, uploads, _LETTER_BLOCK, "Personligt brev", applicant.letter_pdf
                )
            )
        else:
            fields.append(
                KnownField(
                    "Personligt brev",
                    page.locator('textarea[name="candidate[pitch]"]'),
                    applicant.letter_text,
                )
            )
        return fields

    def _upload_inputs(self, page: Page) -> dict[str, str | None]:
        """Block-id -> selektor för blockets filfält (None när kopplingen inte är säker)."""
        blocks: list[str] = page.evaluate(_UPLOAD_BLOCKS_JS)
        inside = {b: f'#{b} input[type="file"]' for b in blocks}
        if all(page.locator(sel).count() for sel in inside.values()):
            return dict(inside)
        # Dropzone-inputs hör ihop med blocken enbart via ordningen: gissa aldrig om
        # antalet inte stämmer.
        if page.locator(_DROPZONE_INPUTS).count() == len(blocks):
            return {b: f"{_DROPZONE_INPUTS} >> nth={i}" for i, b in enumerate(blocks)}
        return {b: None for b in blocks}

    @staticmethod
    def _file_field(
        page: Page, uploads: dict[str, str | None], block: str, label: str, path: Path
    ) -> KnownField:
        selector = uploads[block]
        return KnownField(label, page.locator(selector) if selector else None, path, file=True)
