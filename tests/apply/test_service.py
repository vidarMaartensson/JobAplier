import itertools
import shutil
import threading
from datetime import datetime

import pytest
from fakes import FakeAI, FakeGmail
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.apply.base import SubmitError, SubmitResult
from jobaplier.apply.email import EmailApplier
from jobaplier.apply.letter import LetterResult
from jobaplier.apply.service import (
    AlreadyHandled,
    ApplyService,
    ChannelChanged,
    NoAutoApply,
    emails_sent_today,
)
from jobaplier.config import Config, Paths
from jobaplier.db import session_scope
from jobaplier.gmail import GmailAuthError, GmailError
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobStatus,
)
from jobaplier.profile.cv import Profile

PROFILE = Profile(
    summary="Backendutvecklare",
    skills=["Python"],
    years_experience=6,
    roles=["Backendutvecklare"],
    languages=["sv"],
    education=[],
)


def letter(text: str = "Hej!\n\nJag söker tjänsten.", language: str = "sv") -> LetterResult:
    return LetterResult(language=language, letter=text, unsupported_claims=["Kan Rust"])


class FakeFormApplier:
    """Formulär-dubblett: fyller (låtsas) i och lämnar ansökan för granskning."""

    def __init__(self, *, notes: list[str] | None = None, crash: Exception | None = None):
        self.notes = notes or []
        self.crash = crash
        self.calls: list[tuple[int, int]] = []

    def submit(self, job: Job, draft: Draft) -> SubmitResult:
        self.calls.append((job.id, draft.id))
        if self.crash is not None:
            raise self.crash
        return SubmitResult(
            channel=Channel.form, status=ApplicationStatus.submitting, notes=self.notes
        )


@pytest.fixture
def cv(paths: Paths, make_pdf) -> None:
    shutil.copy(make_pdf("Jane Doe. Backendutvecklare på Acme 2018-2024."), paths.cv)


@pytest.fixture
def ai() -> FakeAI:
    return FakeAI({Profile: lambda _prompt: PROFILE, LetterResult: [letter()]})


@pytest.fixture
def gmail() -> FakeGmail:
    return FakeGmail()


@pytest.fixture
def form() -> FakeFormApplier:
    return FakeFormApplier(notes=["Har du körkort?", "Lönekrav"])


@pytest.fixture
def service(engine, ai, paths, config, gmail, form, cv) -> ApplyService:
    config.apply.dry_run = False
    email = EmailApplier(gmail, lambda: config, paths, lambda: emails_sent_today(engine))
    return ApplyService(
        engine,
        ai,
        paths,
        lambda: config,
        {ApplyMethod.email: email, ApplyMethod.form: form},
    )


_keys = itertools.count()


def add_job(engine, status: JobStatus = JobStatus.scored, **overrides) -> int:
    values = {
        "dedupe_key": f"k{next(_keys)}",
        "title": "Utvecklare",
        "company": "Exempel AB",
        "description": "Vi söker en utvecklare.",
        "apply_method": ApplyMethod.email,
        "apply_email": "jobb@exempel.se",
        "status": status,
    }
    values.update(overrides)
    with session_scope(engine) as session:
        job = Job(**values)
        session.add(job)
        session.flush()
        return job.id


def job_status(engine, job_id: int) -> JobStatus:
    with Session(engine) as session:
        return session.get(Job, job_id).status


def applications(engine, job_id: int) -> list[Application]:
    with Session(engine) as session:
        rows = list(session.exec(select(Application).where(Application.job_id == job_id)))
        for row in rows:
            session.expunge(row)
        return rows


def ready_job(service: ApplyService, engine, **overrides) -> tuple[int, Draft]:
    job_id = add_job(engine, **overrides)
    draft = service.start_draft(job_id)
    return job_id, draft


# --- länk och avfärda ---------------------------------------------------------


def test_link_and_dismiss(service: ApplyService, engine):
    linked = add_job(engine)
    job = service.mark_link(linked)
    assert job.status == JobStatus.link
    assert job.title == "Utvecklare"  # frånkopplad men laddad
    assert job_status(engine, linked) == JobStatus.link

    dismissed = add_job(engine)
    assert service.dismiss(dismissed).status == JobStatus.dismissed
    assert job_status(engine, dismissed) == JobStatus.dismissed

    with pytest.raises(AlreadyHandled, match="avfärdad"):
        service.mark_link(dismissed)
    with pytest.raises(AlreadyHandled):
        service.dismiss(dismissed)


def test_dismiss_draft_ready(service: ApplyService, engine):
    job_id, _ = ready_job(service, engine)
    assert service.dismiss(job_id).status == JobStatus.dismissed


def test_unknown_job_raises_lookup_error(service: ApplyService):
    for call in (
        lambda: service.mark_link(999),
        lambda: service.dismiss(999),
        lambda: service.start_draft(999),
        lambda: service.latest_draft(999),
        lambda: service.approve_and_submit(999, 1),
        lambda: service.mark_submitted(999),
        lambda: service.retry(999),
    ):
        with pytest.raises(LookupError, match="Jobbet finns inte"):
            call()


# --- utkast -----------------------------------------------------------------------


def test_start_draft_creates_version_1(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter("Hello!\n\nI am applying.", language="en")]
    job_id = add_job(engine)
    draft = service.start_draft(job_id)
    assert draft.version == 1
    assert draft.job_id == job_id
    assert draft.language == "en"
    assert draft.letter_text == "Hello!\n\nI am applying."
    assert draft.unsupported_claims_json == ["Kan Rust"]
    assert draft.instruction is None
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert service.latest_draft(job_id).id == draft.id
    assert [c["purpose"] for c in ai.calls] == ["profile", "letter"]


def test_start_draft_ai_error_returns_to_scored(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [AIError("AI-tjänsten svarade inte")]
    job_id = add_job(engine)
    with pytest.raises(AIError, match="svarade inte"):
        service.start_draft(job_id)
    assert job_status(engine, job_id) == JobStatus.scored
    assert service.latest_draft(job_id) is None


def test_start_draft_requires_scored(service: ApplyService, engine):
    job_id, _ = ready_job(service, engine)
    with pytest.raises(AlreadyHandled, match="utkast klart"):
        service.start_draft(job_id)


def test_regenerate_and_edit_increment_version(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter("Första versionen 1111"), letter("Andra versionen 2222")]
    job_id, first = ready_job(service, engine)

    second = service.regenerate(job_id, "Gör den kortare")
    assert second.version == 2
    assert second.instruction == "Gör den kortare"
    assert second.letter_text == "Andra versionen 2222"
    prompt = ai.calls[-1]["prompt"]
    assert "Första versionen 1111" in prompt and "Gör den kortare" in prompt

    third = service.save_edit(job_id, "Mitt eget brev")
    assert third.version == 3
    assert third.instruction == "manuell redigering"
    assert third.unsupported_claims_json == []
    assert third.language == second.language
    assert service.latest_draft(job_id).id == third.id
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert first.version == 1


def test_regenerate_and_edit_require_draft_ready(service: ApplyService, engine):
    job_id = add_job(engine)
    with pytest.raises(AlreadyHandled, match="poängsatt"):
        service.regenerate(job_id, "Kortare")
    with pytest.raises(AlreadyHandled):
        service.save_edit(job_id, "Text")


# --- godkänn och skicka -------------------------------------------------------------


def test_email_happy_path(service: ApplyService, engine, gmail: FakeGmail):
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitted
    assert app.receipt == "fake-1"
    assert app.channel == Channel.email
    assert app.dry_run is False
    assert app.submitted_at is not None
    assert app.draft_id == draft.id
    assert job_status(engine, job_id) == JobStatus.submitted
    assert len(gmail.sent) == 1
    assert gmail.sent[0]["To"] == "jobb@exempel.se"
    assert emails_sent_today(engine) == 1


def test_dry_run_keeps_job_as_draft_then_real_send_reuses_row(
    service: ApplyService, engine, gmail: FakeGmail, config: Config
):
    """En torrkörning förbrukar inte jobbet: det kan skickas på riktigt senare."""
    config.apply.dry_run = True
    job_id, draft = ready_job(service, engine)

    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitted
    assert app.dry_run is True
    assert app.receipt == "fake-1"
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert gmail.sent[0]["To"] == config.profile.email
    assert emails_sent_today(engine) == 0

    config.apply.dry_run = False
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitted
    assert app.dry_run is False
    assert app.receipt == "fake-2"
    assert gmail.sent[1]["To"] == "jobb@exempel.se"
    assert job_status(engine, job_id) == JobStatus.submitted
    (row,) = applications(engine, job_id)  # samma rad (job_id, kanal)
    assert row.id == app.id and row.dry_run is False
    assert emails_sent_today(engine) == 1


def test_application_is_committed_before_applier_runs(service: ApplyService, engine):
    seen: list[tuple[JobStatus, list[ApplicationStatus]]] = []

    class Spy:
        def submit(self, job: Job, draft: Draft) -> SubmitResult:
            statuses = [a.status for a in applications(engine, job.id)]
            seen.append((job_status(engine, job.id), statuses))
            return SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting)

    service.appliers[ApplyMethod.form] = Spy()
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    service.approve_and_submit(job_id, draft.id)
    assert seen == [(JobStatus.submitting, [ApplicationStatus.submitting])]


def test_concurrent_approve_sends_once(engine, ai, paths, config, cv):
    config.apply.dry_run = False
    gmail = FakeGmail(send_delay=0.2)
    email = EmailApplier(gmail, lambda: config, paths, lambda: emails_sent_today(engine))
    service = ApplyService(engine, ai, paths, lambda: config, {ApplyMethod.email: email})
    job_id, draft = ready_job(service, engine)

    barrier = threading.Barrier(2)
    results: list[Application] = []
    errors: list[Exception] = []

    def click() -> None:
        barrier.wait()
        try:
            results.append(service.approve_and_submit(job_id, draft.id))
        except Exception as e:  # noqa: BLE001 - samlas och kontrolleras nedan
            errors.append(e)

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(gmail.sent) == 1
    assert len(results) == 1 and results[0].status == ApplicationStatus.submitted
    assert len(errors) == 1 and isinstance(errors[0], AlreadyHandled)
    assert job_status(engine, job_id) == JobStatus.submitted
    assert len(applications(engine, job_id)) == 1


def test_approve_rejects_channel_changed_since_page_load(
    service: ApplyService, engine, gmail: FakeGmail, form
):
    """Användaren såg "Öppna formulär"; har kanalen bytts till mejl skickas inget."""
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        job.apply_method = ApplyMethod.email
        job.apply_email = "jobb@exempel.se"
    with pytest.raises(ChannelChanged, match="Ansökningsvägen har ändrats") as exc:
        service.approve_and_submit(job_id, draft.id, expected_channel=Channel.form)
    assert isinstance(exc.value, AlreadyHandled)
    assert gmail.sent == [] and form.calls == []
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []


def test_approve_with_matching_channel_sends(service: ApplyService, engine, gmail: FakeGmail):
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id, expected_channel=Channel.email)
    assert app.status == ApplicationStatus.submitted
    assert len(gmail.sent) == 1


def test_approve_twice_sequentially_raises(service: ApplyService, engine, gmail: FakeGmail):
    job_id, draft = ready_job(service, engine)
    service.approve_and_submit(job_id, draft.id)
    with pytest.raises(AlreadyHandled, match="skickad"):
        service.approve_and_submit(job_id, draft.id)
    assert len(gmail.sent) == 1


def test_approve_with_draft_of_other_job(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter(), letter()]
    job_a, _ = ready_job(service, engine)
    _, draft_b = ready_job(service, engine)
    with pytest.raises(LookupError):
        service.approve_and_submit(job_a, draft_b.id)
    with pytest.raises(LookupError):
        service.approve_and_submit(job_a, 999)
    assert job_status(engine, job_a) == JobStatus.draft_ready
    assert applications(engine, job_a) == []


def test_submit_error_marks_failed_then_retry(service: ApplyService, engine, gmail: FakeGmail):
    gmail.fail_send = GmailError("Gmail svarade med fel (HTTP 400).", status=400)
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert app.error == "Gmail svarade med fel (HTTP 400)."
    assert job_status(engine, job_id) == JobStatus.failed
    assert gmail.sent == []

    job = service.retry(job_id)
    assert job.status == JobStatus.draft_ready
    with pytest.raises(AlreadyHandled):
        service.retry(job_id)

    gmail.fail_send = None
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitted
    assert app.error is None
    assert app.receipt == "fake-1"
    (row,) = applications(engine, job_id)  # samma rad återanvänds
    assert row.id == app.id and row.status == ApplicationStatus.submitted


@pytest.mark.parametrize(
    "error",
    [
        GmailError("Mejlet kunde inte skickas via Gmail: nätverksfel (TimeoutError)."),
        GmailError("Mejlet kunde inte skickas via Gmail (HTTP 503).", status=503),
        TimeoutError("timed out"),
    ],
)
def test_ambiguous_send_failure_warns_to_check_gmail(
    service: ApplyService, engine, gmail: FakeGmail, error: Exception
):
    gmail.fail_send = error
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert app.error.startswith("Kontrollera Skickat i Gmail innan du försöker igen: ")
    assert str(error) in app.error
    assert job_status(engine, job_id) == JobStatus.failed


@pytest.mark.parametrize(
    "error",
    [
        GmailError("Mejlet kunde inte skickas via Gmail (HTTP 400).", status=400),
        GmailAuthError("Gmail-inloggningen har gått ut. Kör `jobaplier gmail-login`."),
    ],
)
def test_definite_send_failure_has_no_gmail_warning(
    service: ApplyService, engine, gmail: FakeGmail, error: Exception
):
    gmail.fail_send = error
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert app.error == str(error)
    assert "Kontrollera Skickat" not in app.error


def test_daily_limit_marks_failed(service: ApplyService, engine, config: Config):
    config.apply.max_emails_per_day = 0
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert "tak" in app.error
    assert job_status(engine, job_id) == JobStatus.failed


def test_unexpected_email_error_marks_failed(service: ApplyService, engine):
    class Broken:
        def submit(self, job: Job, draft: Draft) -> SubmitResult:
            raise RuntimeError("oväntat")

    service.appliers[ApplyMethod.email] = Broken()
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert "oväntat" in app.error
    assert job_status(engine, job_id) == JobStatus.failed  # inte draft_ready: kan ha skickats


def test_form_path_stays_submitting_then_mark_submitted(
    service: ApplyService, engine, form: FakeFormApplier
):
    job_id, draft = ready_job(
        service, engine, apply_method=ApplyMethod.form, apply_form_url="https://x.se/jobb"
    )
    app = service.approve_and_submit(job_id, draft.id)
    assert form.calls == [(job_id, draft.id)]
    assert app.channel == Channel.form
    assert app.status == ApplicationStatus.submitting
    assert app.error == "Obesvarade frågor: Har du körkort?, Lönekrav"
    assert job_status(engine, job_id) == JobStatus.submitting

    app = service.mark_submitted(job_id)
    assert app.status == ApplicationStatus.submitted
    assert app.submitted_at is not None
    assert app.dry_run is False
    assert job_status(engine, job_id) == JobStatus.submitted
    assert len(applications(engine, job_id)) == 1
    with pytest.raises(AlreadyHandled):
        service.mark_submitted(job_id)


def test_form_path_without_unanswered_questions(service: ApplyService, engine, form):
    form.notes = []
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitting
    assert app.error is None


def test_form_crash_returns_to_draft_ready(service: ApplyService, engine, form):
    form.crash = RuntimeError("Webbläsaren kraschade")
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert "Webbläsaren kraschade" in app.error
    assert job_status(engine, job_id) == JobStatus.draft_ready

    form.crash = None
    app = service.approve_and_submit(job_id, draft.id)  # går att godkänna igen
    assert app.status == ApplicationStatus.submitting
    assert len(applications(engine, job_id)) == 1


def test_form_submit_error_marks_failed(service: ApplyService, engine, form):
    form.crash = SubmitError("Ingen formuläradapter passar sidan")
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert app.error == "Ingen formuläradapter passar sidan"
    assert job_status(engine, job_id) == JobStatus.failed


def test_unknown_method_raises_no_auto_apply(service: ApplyService, engine, gmail, form):
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.unknown)
    with pytest.raises(NoAutoApply, match="ingen automatisk ansökningsväg"):
        service.approve_and_submit(job_id, draft.id)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []
    assert gmail.sent == [] and form.calls == []


def test_mark_submitted_email_in_flight_is_refused(service: ApplyService, engine):
    job_id = add_job(engine, status=JobStatus.submitting)
    with pytest.raises(AlreadyHandled):
        service.mark_submitted(job_id)
    assert job_status(engine, job_id) == JobStatus.submitting


def test_link_mark_submitted_creates_manual_application(service: ApplyService, engine):
    job_id = add_job(engine, apply_method=ApplyMethod.unknown)
    service.mark_link(job_id)
    app = service.mark_submitted(job_id)
    assert app.channel == Channel.manual
    assert app.status == ApplicationStatus.submitted
    assert app.dry_run is False
    assert app.draft_id is None
    assert app.submitted_at is not None
    assert job_status(engine, job_id) == JobStatus.submitted

    scored = add_job(engine)
    with pytest.raises(AlreadyHandled):
        service.mark_submitted(scored)


# --- dagstak ------------------------------------------------------------------------


def test_emails_sent_today_excludes_dry_run(engine):
    # 2026-10-19 10:00 UTC = 12:00 i Stockholm (CEST); lokal midnatt = 2026-10-18 22:00 UTC.
    now = datetime(2026, 10, 19, 10, 0)
    rows = [
        (Channel.email, ApplicationStatus.submitted, False, datetime(2026, 10, 18, 22, 30)),
        (Channel.email, ApplicationStatus.submitted, False, datetime(2026, 10, 19, 9, 0)),
        (Channel.email, ApplicationStatus.submitted, True, datetime(2026, 10, 19, 9, 0)),
        (Channel.email, ApplicationStatus.submitted, False, datetime(2026, 10, 18, 21, 30)),
        (Channel.email, ApplicationStatus.failed, False, datetime(2026, 10, 19, 9, 0)),
        (Channel.email, ApplicationStatus.submitting, False, None),
        (Channel.form, ApplicationStatus.submitted, False, datetime(2026, 10, 19, 9, 0)),
        (Channel.manual, ApplicationStatus.submitted, False, datetime(2026, 10, 19, 9, 0)),
    ]
    with session_scope(engine) as session:
        for n, (channel, status, dry_run, at) in enumerate(rows):
            job = Job(dedupe_key=f"k{n}", title="T", company="C")
            session.add(job)
            session.flush()
            session.add(
                Application(
                    job_id=job.id,
                    channel=channel,
                    status=status,
                    dry_run=dry_run,
                    submitted_at=at,
                )
            )
    assert emails_sent_today(engine, now) == 2


def test_emails_sent_today_winter_time(engine):
    # Vintertid (CET, UTC+1): lokal midnatt 2026-12-01 = 2026-11-30 23:00 UTC.
    with session_scope(engine) as session:
        for n, at in enumerate([datetime(2026, 11, 30, 22, 59), datetime(2026, 11, 30, 23, 1)]):
            job = Job(dedupe_key=f"w{n}", title="T", company="C")
            session.add(job)
            session.flush()
            session.add(
                Application(
                    job_id=job.id,
                    channel=Channel.email,
                    status=ApplicationStatus.submitted,
                    dry_run=False,
                    submitted_at=at,
                )
            )
    assert emails_sent_today(engine, datetime(2026, 12, 1, 10, 0)) == 1
