"""Gemensamma typer för ansökningskanaler (formulär)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from jobaplier.models import ApplicationStatus, Channel, Draft, Job


@dataclass
class SubmitResult:
    channel: Channel
    status: ApplicationStatus
    receipt: str | None = None
    notes: list[str] = field(default_factory=list)


class SubmitError(Exception):
    """Ansökan kunde inte skickas. Meddelandet är på svenska och visas för användaren."""


class Applier(Protocol):
    def submit(self, job: Job, draft: Draft) -> SubmitResult: ...
