"""Gemensamma typer för ansökningskanaler (mejl, formulär)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from jobaplier.models import ApplicationStatus, Channel, Draft, Job


@dataclass
class SubmitResult:
    channel: Channel
    status: ApplicationStatus
    receipt: str | None = None
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)


class SubmitError(Exception):
    """Ansökan kunde inte skickas. Meddelandet är på svenska och visas för användaren."""


class AmbiguousSendError(SubmitError):
    """Det är okänt om ansökan gick iväg (t.ex. tidsgräns efter att Gmail tagit emot
    mejlet). Användaren måste kontrollera innan ett nytt försök."""


class DailyLimitReached(SubmitError):
    """Dagens tak för antal mejlansökningar är nått. Inget har skickats."""


class Applier(Protocol):
    def submit(self, job: Job, draft: Draft) -> SubmitResult: ...
