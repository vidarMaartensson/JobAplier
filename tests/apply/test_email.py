import shutil
from pathlib import Path

import pytest
from fakes import FakeGmail

from jobaplier.apply.base import (
    AmbiguousSendError,
    DailyLimitReached,
    SubmitError,
    SubmitResult,
)
from jobaplier.apply.email import EmailApplier
from jobaplier.config import Config, Paths
from jobaplier.gmail import GmailAuthError, GmailError
from jobaplier.models import ApplicationStatus, Channel, Draft, Job


@pytest.fixture
def cv_bytes(paths: Paths, make_pdf) -> bytes:
    shutil.copy(make_pdf("Mitt CV"), paths.cv)
    return paths.cv.read_bytes()


def make_job(**overrides) -> Job:
    values = dict(
        dedupe_key="k",
        title="Utvecklare",
        company="Exempel AB",
        apply_email="jobb@exempel.se",
    )
    values.update(overrides)
    return Job(**values)


def make_draft(language: str = "sv") -> Draft:
    return Draft(job_id=1, letter_text="Hej!\n\nJag söker tjänsten.", language=language)


def make_applier(
    gmail: FakeGmail, config: Config, paths: Paths, sent_today: int = 0
) -> EmailApplier:
    return EmailApplier(gmail, lambda: config, paths, lambda: sent_today)


def test_recipient_subject_body(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    config.profile.phone = "070-123 45 67"
    gmail = FakeGmail()
    result = make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert result == SubmitResult(
        channel=Channel.email,
        status=ApplicationStatus.submitted,
        receipt="fake-1",
        dry_run=False,
    )
    (msg,) = gmail.sent
    assert msg["To"] == "jobb@exempel.se"
    assert msg["From"] == "test@example.se"
    assert msg["Subject"] == "Ansökan: Utvecklare"
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert body.strip() == (
        "Hej!\n\nHär kommer min ansökan till tjänsten Utvecklare. "
        "CV och personligt brev bifogas.\n\nVänliga hälsningar\n"
        "Test Testsson\n070-123 45 67"
    )


def test_subject_with_reference(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    gmail = FakeGmail()
    make_applier(gmail, config, paths).submit(
        make_job(apply_reference="ABC-123"), make_draft()
    )
    assert gmail.sent[0]["Subject"] == "Ansökan: Utvecklare (ref. ABC-123)"


def test_attachments(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    gmail = FakeGmail()
    make_applier(gmail, config, paths).submit(make_job(), make_draft())
    attachments = list(gmail.sent[0].iter_attachments())
    assert [a.get_filename() for a in attachments] == ["CV.pdf", "Personligt_brev.pdf"]
    assert all(a.get_content_type() == "application/pdf" for a in attachments)
    assert attachments[0].get_content() == cv_bytes
    letter_pdf = attachments[1].get_content()
    assert letter_pdf.startswith(b"%PDF")
    assert letter_pdf != cv_bytes


def test_dry_run_redirects_to_self(config: Config, paths: Paths, cv_bytes):
    assert config.apply.dry_run is True
    gmail = FakeGmail()
    result = make_applier(gmail, config, paths).submit(
        make_job(apply_reference="R1"), make_draft()
    )
    assert result.dry_run is True
    assert result.status == ApplicationStatus.submitted
    (msg,) = gmail.sent
    assert msg["To"] == "test@example.se"
    assert msg["Subject"] == (
        "[TORRKÖRNING → jobb@exempel.se] Ansökan: Utvecklare (ref. R1)"
    )
    assert "jobb@exempel.se" not in str(msg["To"])


def test_daily_limit_blocks_before_send(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    config.apply.max_emails_per_day = 3
    gmail = FakeGmail()
    with pytest.raises(DailyLimitReached) as exc:
        make_applier(gmail, config, paths, sent_today=3).submit(make_job(), make_draft())
    assert str(exc.value) == "Dagens tak på 3 mejlansökningar är nått"
    assert isinstance(exc.value, SubmitError)
    assert gmail.sent == []


def test_below_limit_sends(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    config.apply.max_emails_per_day = 3
    gmail = FakeGmail()
    make_applier(gmail, config, paths, sent_today=2).submit(make_job(), make_draft())
    assert len(gmail.sent) == 1


def test_dry_run_ignores_limit(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = True
    config.apply.max_emails_per_day = 1
    gmail = FakeGmail()
    result = make_applier(gmail, config, paths, sent_today=50).submit(
        make_job(), make_draft()
    )
    assert result.dry_run is True
    assert len(gmail.sent) == 1


@pytest.mark.parametrize(
    "error",
    [
        GmailError("Gmail sa nej (HTTP 400).", status=400),
        GmailError("För många anrop (HTTP 429).", status=429),
        GmailAuthError("Inloggningen har gått ut"),
    ],
)
def test_definitely_not_sent_gmail_error_becomes_submit_error(
    config: Config, paths: Paths, cv_bytes, error: Exception
):
    """Gmail svarade med 4xx eller inloggningen saknas: mejlet gick inte iväg."""
    config.apply.dry_run = False
    gmail = FakeGmail(fail_send=error)
    with pytest.raises(SubmitError) as exc:
        make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert not isinstance(exc.value, AmbiguousSendError)
    assert str(exc.value) == str(error)
    assert exc.value.__cause__ is error


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (GmailError("Mejlet kunde inte skickas via Gmail: nätverksfel"), "nätverksfel"),
        (GmailError("Mejlet kunde inte skickas via Gmail (HTTP 500).", status=500), "HTTP 500"),
        (ConnectionError("nätverket låg nere"), "ConnectionError: nätverket låg nere"),
    ],
)
def test_unknown_send_outcome_becomes_ambiguous_send_error(
    config: Config, paths: Paths, cv_bytes, error: Exception, detail: str
):
    """Tidsgräns, nätverksfel, 5xx eller okänt fel: Gmail kan ha tagit emot mejlet."""
    config.apply.dry_run = False
    gmail = FakeGmail(fail_send=error)
    with pytest.raises(AmbiguousSendError) as exc:
        make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert isinstance(exc.value, SubmitError)
    assert detail in str(exc.value)
    assert exc.value.__cause__ is error


def test_english_body(config: Config, paths: Paths, cv_bytes):
    config.apply.dry_run = False
    config.profile.phone = "+46 70 123 45 67"
    gmail = FakeGmail()
    make_applier(gmail, config, paths).submit(make_job(title="Engineer"), make_draft("en"))
    body = gmail.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert body.strip() == (
        "Hello,\n\nPlease find attached my application for the Engineer position, "
        "including my CV and cover letter.\n\nBest regards\nTest Testsson\n"
        "+46 70 123 45 67"
    )


def test_missing_apply_email(config: Config, paths: Paths, cv_bytes):
    gmail = FakeGmail()
    with pytest.raises(SubmitError, match="Jobbet saknar mejladress för ansökan"):
        make_applier(gmail, config, paths).submit(make_job(apply_email=None), make_draft())
    assert gmail.sent == []


def test_missing_cv(config: Config, paths: Paths):
    assert not paths.cv.exists()
    gmail = FakeGmail()
    with pytest.raises(SubmitError, match="CV"):
        make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert gmail.sent == []


def test_config_read_per_submit(config: Config, paths: Paths, cv_bytes):
    gmail = FakeGmail()
    current = config.model_copy(deep=True)
    applier = EmailApplier(gmail, lambda: current, paths, lambda: 0)
    applier.submit(make_job(), make_draft())
    assert gmail.sent[0]["To"] == "test@example.se"
    current.apply.dry_run = False
    applier.submit(make_job(), make_draft())
    assert gmail.sent[1]["To"] == "jobb@exempel.se"


def test_temp_dir_removed(config: Config, paths: Paths, cv_bytes, monkeypatch):
    seen: list[Path] = []
    from jobaplier.apply import email as email_module

    real = email_module.render_letter_pdf

    def spy(letter, name, out, **kw):
        seen.append(out)
        return real(letter, name, out, **kw)

    monkeypatch.setattr(email_module, "render_letter_pdf", spy)
    make_applier(FakeGmail(), config, paths).submit(make_job(), make_draft())
    assert len(seen) == 1
    assert not seen[0].exists()
    assert not seen[0].parent.exists()


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "address",
    ["a@b.se\nBcc: x@y.se", "a@b.se, c@d.se", "a@b.se;c@d.se", "inte en adress",
     "Namn <a@b.se>", "a@b"],
)
def test_invalid_apply_email_rejected(
    config: Config, paths: Paths, cv_bytes, address: str, dry_run: bool
):
    config.apply.dry_run = dry_run
    gmail = FakeGmail()
    with pytest.raises(SubmitError, match="Mejladressen i annonsen är ogiltig"):
        make_applier(gmail, config, paths).submit(
            make_job(apply_email=address), make_draft()
        )
    assert gmail.sent == []


def test_invalid_email_checked_before_cap_and_cv(config: Config, paths: Paths):
    config.apply.dry_run = False
    config.apply.max_emails_per_day = 1
    with pytest.raises(SubmitError, match="ogiltig") as exc:
        make_applier(FakeGmail(), config, paths, sent_today=5).submit(
            make_job(apply_email="a@b.se, c@d.se"), make_draft()
        )
    assert not isinstance(exc.value, DailyLimitReached)


def test_cap_checked_before_cv(config: Config, paths: Paths):
    config.apply.dry_run = False
    config.apply.max_emails_per_day = 2
    assert not paths.cv.exists()
    gmail = FakeGmail()
    with pytest.raises(DailyLimitReached):
        make_applier(gmail, config, paths, sent_today=2).submit(make_job(), make_draft())
    assert gmail.sent == []


def test_render_failure_becomes_submit_error(
    config: Config, paths: Paths, cv_bytes, monkeypatch
):
    from jobaplier.apply import email as email_module

    boom = RuntimeError("pango saknas")

    def failing(*args, **kwargs):
        raise boom

    monkeypatch.setattr(email_module, "render_letter_pdf", failing)
    gmail = FakeGmail()
    with pytest.raises(SubmitError, match="pango saknas") as exc:
        make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert exc.value.__cause__ is boom
    assert gmail.sent == []


def test_unreadable_cv_becomes_submit_error(
    config: Config, paths: Paths, cv_bytes, monkeypatch
):
    original = Path.read_bytes

    def fail_for_cv(self: Path) -> bytes:
        if self == paths.cv:
            raise PermissionError("nekad")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fail_for_cv)
    gmail = FakeGmail()
    with pytest.raises(SubmitError, match="nekad") as exc:
        make_applier(gmail, config, paths).submit(make_job(), make_draft())
    assert isinstance(exc.value.__cause__, PermissionError)
    assert gmail.sent == []
