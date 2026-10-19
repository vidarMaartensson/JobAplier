"""Gemensamma typer för jobbkällor."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jobaplier.models import ApplyMethod


class SourceError(Exception):
    """En källa kunde inte hämtas. Meddelandet är på svenska och visas för användaren."""


@dataclass
class ApplyInfo:
    method: ApplyMethod
    email: str | None = None
    form_url: str | None = None
    reference: str | None = None


@dataclass
class RawJob:
    source: str
    external_id: str
    title: str
    company: str
    location: str | None
    remote: bool | None
    description: str
    url: str
    published_at: datetime | None
    apply: ApplyInfo
    incomplete: bool = False


class Source(Protocol):
    name: str

    def fetch(self, since: datetime) -> list[RawJob]: ...
