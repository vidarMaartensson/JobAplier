"""Sökandens uppgifter och klassning av formulärfält utifrån etikett och attribut."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jobaplier.config import Config, Paths

# Regex (casefold) mot etikett/name/id/placeholder/aria-label. Ordningen avgör vid
# flera träffar.
FIELD_PATTERNS: dict[str, list[str]] = {
    "first_name": ["förnamn", "first.?name", "given.?name"],
    "last_name": ["efternamn", "last.?name", "surname", "family.?name"],
    "full_name": ["^namn$", "full.?name", "^name$", "ditt namn"],
    "email": ["e-?post", "e-?mail", "mejl"],
    "phone": ["telefon", "mobil", "phone"],
    "linkedin": ["linkedin"],
    "cv": ["\\bcv\\b", "resume", "résumé", "curriculum"],
    "letter": ["personligt brev", "cover.?letter", "motivation"],
}

_COMPILED = {
    kind: [re.compile(p) for p in patterns] for kind, patterns in FIELD_PATTERNS.items()
}


def classify_field(descriptor: str) -> str | None:
    text = descriptor.casefold().strip()
    for kind, patterns in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            return kind
    return None


@dataclass
class Applicant:
    first_name: str
    last_name: str
    email: str
    phone: str
    linkedin_url: str
    cv_path: Path
    letter_text: str
    letter_pdf: Path

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @classmethod
    def from_config(
        cls, config: Config, paths: Paths, letter_text: str, letter_pdf: Path
    ) -> Applicant:
        first, _, last = config.profile.name.strip().partition(" ")
        return cls(
            first_name=first,
            last_name=last.strip(),
            email=config.profile.email,
            phone=config.profile.phone,
            linkedin_url=config.profile.linkedin_url,
            cv_path=paths.cv,
            letter_text=letter_text,
            letter_pdf=letter_pdf,
        )
