"""Skickar ansökan som mejl via Gmail, med torrkörning och dagstak."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from jobaplier.apply.base import (
    AmbiguousSendError,
    DailyLimitReached,
    SubmitError,
    SubmitResult,
)
from jobaplier.apply.pdf import render_letter_pdf
from jobaplier.config import Config, Paths
from jobaplier.gmail import Gmail, GmailAuthError, GmailError
from jobaplier.models import ApplicationStatus, Channel, Draft, Job

BODY_SV = (
    "Hej!\n\nHär kommer min ansökan till tjänsten {title}. "
    "CV och personligt brev bifogas.\n\nVänliga hälsningar\n{name}\n{phone}"
)
BODY_EN = (
    "Hello,\n\nPlease find attached my application for the {title} position, "
    "including my CV and cover letter.\n\nBest regards\n{name}\n{phone}"
)


_ADDRESS_RE = re.compile(r"[^\s,;<>@]+@[^\s,;<>@]+\.[^\s,;<>@]+")


def _valid_single_address(address: str) -> bool:
    """Exakt en ren adress: inga radbrytningar, listor eller visningsnamn."""
    return parseaddr(address)[1] == address and _ADDRESS_RE.fullmatch(address) is not None


def _one_line(text: str) -> str:
    """Rubriker får inte innehålla radbrytningar (annonstexter kan göra det)."""
    return " ".join(text.split())


def application_subject(job: Job) -> str:
    subject = f"Ansökan: {_one_line(job.title)}"
    if job.apply_reference:
        subject += f" (ref. {_one_line(job.apply_reference)})"
    return subject


def build_application_email(
    job: Job,
    draft: Draft,
    config: Config,
    paths: Paths,
    *,
    to: str,
    subject: str,
    letter_pdf: Path,
) -> EmailMessage:
    template = BODY_EN if draft.language == "en" else BODY_SV
    body = template.format(
        title=job.title, name=config.profile.name, phone=config.profile.phone
    )
    msg = EmailMessage()
    msg["From"] = config.profile.email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for source, filename in ((paths.cv, "CV.pdf"), (letter_pdf, "Personligt_brev.pdf")):
        msg.add_attachment(
            Path(source).read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return msg


def _send_error(e: Exception) -> SubmitError:
    """Bara fel där mejlet bevisligen inte gick iväg (inloggning saknas, Gmail svarade
    4xx) är vanliga fel. Tidsgräns, nätverksfel, 5xx och okända fel kan ha kommit efter
    att Gmail tagit emot mejlet."""
    if isinstance(e, GmailAuthError):
        return SubmitError(str(e))
    if isinstance(e, GmailError):
        if e.status is not None and 400 <= e.status < 500:
            return SubmitError(str(e))
        return AmbiguousSendError(str(e))
    return AmbiguousSendError(f"{type(e).__name__}: {e}")


class EmailApplier:
    def __init__(
        self,
        gmail: Gmail,
        config: Callable[[], Config],
        paths: Paths,
        sent_today: Callable[[], int],
    ) -> None:
        self.gmail = gmail
        self.config = config
        self.paths = paths
        self.sent_today = sent_today

    def submit(self, job: Job, draft: Draft) -> SubmitResult:
        if not job.apply_email:
            raise SubmitError("Jobbet saknar mejladress för ansökan")
        if not _valid_single_address(job.apply_email):
            raise SubmitError("Mejladressen i annonsen är ogiltig")
        config = self.config()
        dry_run = config.apply.dry_run
        limit = config.apply.max_emails_per_day
        if not dry_run and self.sent_today() >= limit:
            raise DailyLimitReached(f"Dagens tak på {limit} mejlansökningar är nått")
        if not self.paths.cv.is_file():
            raise SubmitError("Ditt CV saknas (data/cv.pdf) – ladda upp det först")

        subject = application_subject(job)
        to = job.apply_email
        if dry_run:
            subject = f"[TORRKÖRNING → {to}] {subject}"
            to = config.profile.email

        with tempfile.TemporaryDirectory() as tmp:
            try:
                letter_pdf = render_letter_pdf(
                    draft.letter_text,
                    config.profile.name,
                    Path(tmp) / "Personligt_brev.pdf",
                )
            except Exception as e:
                raise SubmitError(f"Kunde inte skapa det personliga brevet som PDF: {e}") from e
            try:
                msg = build_application_email(
                    job,
                    draft,
                    config,
                    self.paths,
                    to=to,
                    subject=subject,
                    letter_pdf=letter_pdf,
                )
            except OSError as e:
                raise SubmitError(f"Kunde inte läsa bilagorna till mejlet: {e}") from e
        try:
            receipt = self.gmail.send(msg)
        except Exception as e:
            raise _send_error(e) from e
        return SubmitResult(
            channel=Channel.email,
            status=ApplicationStatus.submitted,
            receipt=receipt,
            dry_run=dry_run,
        )
