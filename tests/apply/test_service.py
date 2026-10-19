import itertools
import shutil
import threading
import time
import pytest
from fakes import FakeAI
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.apply.base import SubmitError, SubmitResult
from jobaplier.apply.letter import LetterResult
from jobaplier.apply.tailor import TailoredCV, TailoredExperience
from jobaplier.apply.service import (
    AlreadyHandled,
    ApplyService,
    ChannelChanged,
    NoAutoApply,
)
from jobaplier.config import Paths
from jobaplier.db import session_scope
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobStatus,
    TailoredCVRecord,
)
from jobaplier.profile.cv import CVMissing, Profile, cv_file_hash
from jobaplier.profile.cv_document import CVDocument, Experience

PROFILE = Profile(
    summary="Backendutvecklare",
    skills=["Python"],
    years_experience=6,
    roles=["Backendutvecklare"],
    languages=["sv"],
    education=[],
)


DOC = CVDocument(
    name="Jane Doe",
    experience=[
        Experience(
            id="e1",
            title="Backendutvecklare",
            employer="Acme",
            period="2018 – 2024",
            bullets=["Byggde API:er i Python"],
        )
    ],
    skills=["Python"],
)


def tailored(bullet: str = "Byggde API:er i Python för 3 team") -> TailoredCV:
    return TailoredCV(
        language="sv",
        headline="Backendutvecklare",
        summary="Anpassad profil.",
        experience=[TailoredExperience(id="e1", bullets=[bullet])],
        skills=["Python", "Rust"],
        education_order=[],
    )


def letter(text: str = "Hej!\n\nJag söker tjänsten.", language: str = "sv") -> LetterResult:
    return LetterResult(language=language, letter=text, unsupported_claims=["Kan Rust"])


class FakeFormApplier:
    """Formulär-dubblett: fyller (låtsas) i och lämnar ansökan för granskning."""

    def __init__(
        self,
        *,
        notes: list[str] | None = None,
        crash: Exception | None = None,
        delay: float = 0.0,
    ):
        self.notes = notes or []
        self.crash = crash
        self.delay = delay
        self.calls: list[tuple[int, int]] = []

    def submit(self, job: Job, draft: Draft) -> SubmitResult:
        self.calls.append((job.id, draft.id))
        time.sleep(self.delay)
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
    return FakeAI(
        {
            Profile: lambda _prompt: PROFILE,
            LetterResult: [letter()],
            CVDocument: lambda _prompt: DOC,
            TailoredCV: lambda _prompt: tailored(),
        }
    )


@pytest.fixture
def form() -> FakeFormApplier:
    return FakeFormApplier(notes=["Har du körkort?", "Lönekrav"])


@pytest.fixture
def service(engine, ai, paths, config, form, cv) -> ApplyService:
    return ApplyService(engine, ai, paths, lambda: config, {ApplyMethod.form: form})


_keys = itertools.count()


def add_job(engine, status: JobStatus = JobStatus.scored, **overrides) -> int:
    values = {
        "dedupe_key": f"k{next(_keys)}",
        "title": "Utvecklare",
        "company": "Exempel AB",
        "description": "Vi söker en utvecklare.",
        "apply_method": ApplyMethod.form,
        "apply_form_url": "https://x.se/jobb",
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


def prompt_of(ai: FakeAI, purpose: str) -> str:
    return [c["prompt"] for c in ai.calls if c["purpose"] == purpose][-1]


def tailored_rows(engine) -> list[TailoredCVRecord]:
    with Session(engine) as session:
        rows = list(session.exec(select(TailoredCVRecord).order_by(TailoredCVRecord.id)))
        for row in rows:
            session.expunge(row)
        return rows


def drafts_of(engine, job_id: int) -> list[Draft]:
    with Session(engine) as session:
        return list(session.exec(select(Draft).where(Draft.job_id == job_id)))


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
        lambda: service.retry_tailor(999),
        lambda: service.mark_sent_manually(999, 1),
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
    assert [c["purpose"] for c in ai.calls] == ["profile", "letter", "cv_document", "tailor_cv"]


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
    prompt = prompt_of(ai, "letter")
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


def test_concurrent_approve_opens_form_once(engine, ai, paths, config, cv):
    form = FakeFormApplier(delay=0.2)
    service = ApplyService(engine, ai, paths, lambda: config, {ApplyMethod.form: form})
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

    assert form.calls == [(job_id, draft.id)]
    assert len(results) == 1 and results[0].status == ApplicationStatus.submitting
    assert len(errors) == 1 and isinstance(errors[0], AlreadyHandled)
    assert job_status(engine, job_id) == JobStatus.submitting
    assert len(applications(engine, job_id)) == 1


def test_approve_rejects_channel_changed_since_page_load(
    service: ApplyService, engine, form
):
    """Sidan sades höra till en annan kanal än den jobbet har nu: inget öppnas."""
    job_id, draft = ready_job(service, engine)
    with pytest.raises(ChannelChanged, match="Ansökningsvägen har ändrats") as exc:
        service.approve_and_submit(job_id, draft.id, expected_channel=Channel.email)
    assert isinstance(exc.value, AlreadyHandled)
    assert form.calls == []
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []


def test_approve_with_matching_channel_opens_form(service: ApplyService, engine, form):
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id, expected_channel=Channel.form)
    assert app.status == ApplicationStatus.submitting
    assert len(form.calls) == 1


def test_approve_twice_sequentially_raises(service: ApplyService, engine, form):
    job_id, draft = ready_job(service, engine)
    service.approve_and_submit(job_id, draft.id)
    with pytest.raises(AlreadyHandled, match="skickas"):
        service.approve_and_submit(job_id, draft.id)
    assert len(form.calls) == 1


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


def test_submit_error_marks_failed_then_retry(service: ApplyService, engine, form):
    form.crash = SubmitError("Ingen formuläradapter passar sidan")
    job_id, draft = ready_job(service, engine)
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.failed
    assert app.error == "Ingen formuläradapter passar sidan"
    assert job_status(engine, job_id) == JobStatus.failed

    job = service.retry(job_id)
    assert job.status == JobStatus.draft_ready
    with pytest.raises(AlreadyHandled):
        service.retry(job_id)

    form.crash = None
    app = service.approve_and_submit(job_id, draft.id)
    assert app.status == ApplicationStatus.submitting
    assert app.error == "Obesvarade frågor: Har du körkort?, Lönekrav"
    (row,) = applications(engine, job_id)  # samma rad återanvänds
    assert row.id == app.id


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


def test_approve_email_job_raises_no_auto_apply(service: ApplyService, engine, form):
    """Mejl skickas inte längre av programmet; paketet skickar användaren själv."""
    job_id, draft = ready_job(
        service, engine, apply_method=ApplyMethod.email, apply_email="jobb@exempel.se"
    )
    with pytest.raises(NoAutoApply, match="ingen automatisk ansökningsväg"):
        service.approve_and_submit(job_id, draft.id)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []
    assert form.calls == []


def test_unknown_method_raises_no_auto_apply(service: ApplyService, engine, form):
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.unknown)
    with pytest.raises(NoAutoApply, match="ingen automatisk ansökningsväg"):
        service.approve_and_submit(job_id, draft.id)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []
    assert form.calls == []


def test_mark_submitted_email_in_flight_is_refused(service: ApplyService, engine):
    job_id = add_job(engine, status=JobStatus.submitting, apply_method=ApplyMethod.email)
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


# --- skräddarsytt CV ------------------------------------------------------------------


def test_start_draft_creates_letter_and_cv(service: ApplyService, engine, ai: FakeAI, paths):
    job_id, draft = ready_job(service, engine)
    (row,) = tailored_rows(engine)
    assert row.draft_id == draft.id
    assert row.cv_hash == cv_file_hash(paths.cv)
    assert TailoredCV.model_validate(row.tailored_json) == tailored()
    assert row.warnings_json == [
        "Kontrollera siffran 3 i: Byggde API:er i Python för 3 team",
        "Kompetens som inte finns i ditt CV togs bort: Rust",
    ]
    assert [c["purpose"] for c in ai.calls][-3:] == ["letter", "cv_document", "tailor_cv"]
    assert job_status(engine, job_id) == JobStatus.draft_ready

    t, warnings = service.tailored_for(draft.id)
    assert t == tailored()
    assert warnings == row.warnings_json


def test_start_draft_without_cv_when_tailoring_fails(
    service: ApplyService, engine, ai: FakeAI, caplog
):
    ai.responses[TailoredCV] = [AIError("AI-tjänsten svarade inte")]
    job_id, draft = ready_job(service, engine)
    assert draft.version == 1
    assert service.latest_draft(job_id).id == draft.id
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert tailored_rows(engine) == []
    assert service.tailored_for(draft.id) is None
    assert "svarade inte" in caplog.text


def test_start_draft_survives_unexpected_tailoring_error(
    service: ApplyService, engine, ai: FakeAI, caplog
):
    ai.responses[TailoredCV] = [KeyError("oväntat")]
    job_id, draft = ready_job(service, engine)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert service.tailored_for(draft.id) is None
    (record,) = [r for r in caplog.records if "kunde inte anpassas" in r.getMessage()]
    assert record.exc_info is not None  # med traceback i loggen


def test_start_draft_without_cv_when_document_fails(service: ApplyService, engine, ai: FakeAI):
    ai.responses[CVDocument] = [AIError("Struktureringen misslyckades")]
    job_id, _ = ready_job(service, engine)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert tailored_rows(engine) == []


def test_tailoring_starts_after_job_is_draft_ready(service: ApplyService, engine, ai: FakeAI):
    seen: list[tuple[JobStatus, int]] = []

    def spy(_prompt: str) -> TailoredCV:
        seen.append((job_status(engine, job_id), len(drafts_of(engine, job_id))))
        return tailored()

    ai.responses[TailoredCV] = spy
    job_id = add_job(engine)
    service.start_draft(job_id)
    assert seen == [(JobStatus.draft_ready, 1)]


def test_regenerate_creates_both_with_instruction(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter(), letter("Kortare brev")]
    job_id, first = ready_job(service, engine)
    ai.responses[TailoredCV] = [tailored("Ledde backendteamet")]

    second = service.regenerate(job_id, "Betona ledarskap")
    assert "Betona ledarskap" in prompt_of(ai, "letter")
    assert "Betona ledarskap" in prompt_of(ai, "tailor_cv")
    rows = tailored_rows(engine)
    assert [r.draft_id for r in rows] == [first.id, second.id]
    t, _ = service.tailored_for(second.id)
    assert t.experience[0].bullets == ["Ledde backendteamet"]


def test_regenerate_keeps_letter_when_tailoring_fails(
    service: ApplyService, engine, ai: FakeAI
):
    ai.responses[LetterResult] = [letter(), letter("Kortare brev")]
    job_id, _ = ready_job(service, engine)
    ai.responses[TailoredCV] = [AIError("AI-tjänsten svarade inte")]
    second = service.regenerate(job_id, "Kortare")
    assert second.letter_text == "Kortare brev"
    assert service.tailored_for(second.id) is None


def test_save_edit_copies_tailored_cv(service: ApplyService, engine, ai: FakeAI):
    job_id, first = ready_job(service, engine)
    calls = len(ai.calls)
    edited = service.save_edit(job_id, "Mitt eget brev")
    assert len(ai.calls) == calls  # ingen ny anpassning
    original, copy = tailored_rows(engine)
    assert copy.draft_id == edited.id
    assert copy.tailored_json == original.tailored_json
    assert copy.warnings_json == original.warnings_json
    assert copy.cv_hash == original.cv_hash
    assert service.tailored_for(edited.id) == service.tailored_for(first.id)


def test_save_edit_without_tailored_cv(service: ApplyService, engine, ai: FakeAI):
    ai.responses[TailoredCV] = [AIError("fel")]
    job_id, _ = ready_job(service, engine)
    edited = service.save_edit(job_id, "Mitt eget brev")
    assert edited.version == 2
    assert tailored_rows(engine) == []


def test_retry_tailor_adds_row(service: ApplyService, engine, ai: FakeAI):
    ai.responses[TailoredCV] = [AIError("fel"), tailored("Ny punkt")]
    job_id, draft = ready_job(service, engine)
    assert tailored_rows(engine) == []

    result = service.retry_tailor(job_id)
    assert result.id == draft.id
    (row,) = tailored_rows(engine)
    assert row.draft_id == draft.id
    assert service.tailored_for(draft.id)[0].experience[0].bullets == ["Ny punkt"]


def test_retry_tailor_replaces_row_for_latest_version(service: ApplyService, engine, ai: FakeAI):
    ai.responses[TailoredCV] = [tailored("Första"), tailored("Andra")]
    job_id, draft = ready_job(service, engine)
    service.retry_tailor(job_id)
    (row,) = tailored_rows(engine)
    assert row.draft_id == draft.id
    assert service.tailored_for(draft.id)[0].experience[0].bullets == ["Andra"]


def test_retry_tailor_uses_last_real_instruction(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter(), letter()]
    job_id, _ = ready_job(service, engine)
    service.regenerate(job_id, "Betona ledarskap")
    edited = service.save_edit(job_id, "Mitt eget brev")
    service.retry_tailor(job_id)
    prompt = prompt_of(ai, "tailor_cv")
    assert "Betona ledarskap" in prompt and "manuell redigering" not in prompt
    assert tailored_rows(engine)[-1].draft_id == edited.id


def test_retry_tailor_raises_on_ai_error(service: ApplyService, engine, ai: FakeAI):
    job_id, draft = ready_job(service, engine)
    ai.responses[TailoredCV] = [AIError("AI-tjänsten svarade inte")]
    with pytest.raises(AIError, match="svarade inte"):
        service.retry_tailor(job_id)
    (row,) = tailored_rows(engine)  # den gamla raden finns kvar
    assert row.draft_id == draft.id
    assert job_status(engine, job_id) == JobStatus.draft_ready


def test_retry_tailor_raises_when_cv_missing(service: ApplyService, engine, paths):
    job_id, _ = ready_job(service, engine)
    paths.cv.unlink()
    with pytest.raises(CVMissing):
        service.retry_tailor(job_id)


def test_retry_tailor_requires_draft_ready(service: ApplyService, engine):
    job_id = add_job(engine)
    with pytest.raises(AlreadyHandled, match="poängsatt"):
        service.retry_tailor(job_id)


def test_tailored_for_reports_replaced_cv(service: ApplyService, engine, paths, make_pdf):
    _, draft = ready_job(service, engine)
    shutil.copy(make_pdf("Jane Doe. Ny tjänst på Beta 2024 – nu."), paths.cv)
    t, warnings = service.tailored_for(draft.id)
    assert t == tailored()
    # Anpassningens varningar gäller det gamla CV:t och visas inte längre.
    assert warnings == [
        "Ditt CV har bytts sedan utkastet skapades – klicka Försök igen för ett nytt "
        "skräddarsytt CV"
    ]


# --- manuellt skickad -----------------------------------------------------------------


def test_mark_sent_manually_email(service: ApplyService, engine):
    job_id, draft = ready_job(
        service, engine, apply_method=ApplyMethod.email, apply_email="jobb@exempel.se"
    )
    app = service.mark_sent_manually(job_id, draft.id)
    assert app.channel == Channel.email
    assert app.status == ApplicationStatus.submitted
    assert app.draft_id == draft.id
    assert app.dry_run is False
    assert app.submitted_at is not None
    assert job_status(engine, job_id) == JobStatus.submitted
    assert len(applications(engine, job_id)) == 1
    with pytest.raises(AlreadyHandled, match="skickad"):
        service.mark_sent_manually(job_id, draft.id)


def test_mark_sent_manually_reuses_old_email_application(service: ApplyService, engine):
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.email)
    with session_scope(engine) as session:
        session.add(
            Application(
                job_id=job_id,
                channel=Channel.email,
                status=ApplicationStatus.failed,
                error="Gammalt mejlfel",
                dry_run=True,
            )
        )
    app = service.mark_sent_manually(job_id, draft.id)
    (row,) = applications(engine, job_id)
    assert row.id == app.id
    assert row.status == ApplicationStatus.submitted
    assert row.error is None
    assert row.dry_run is False
    assert row.draft_id == draft.id


def test_mark_sent_manually_unknown(service: ApplyService, engine):
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.unknown)
    app = service.mark_sent_manually(job_id, draft.id)
    assert app.channel == Channel.manual
    assert app.status == ApplicationStatus.submitted
    assert job_status(engine, job_id) == JobStatus.submitted


def test_mark_sent_manually_stale_version(service: ApplyService, engine):
    """Review Focus 2: en gammal flik visar version 1 medan version 2 finns."""
    job_id, first = ready_job(service, engine, apply_method=ApplyMethod.email)
    service.save_edit(job_id, "Nyare brev")
    with pytest.raises(AlreadyHandled, match="En nyare version av utkastet finns – ladda om"):
        service.mark_sent_manually(job_id, first.id)
    assert job_status(engine, job_id) == JobStatus.draft_ready
    assert applications(engine, job_id) == []


def test_mark_sent_manually_form_job_uses_manual_channel(service: ApplyService, engine, form):
    """Formulärjobb kan skickas för hand (t.ex. via Öppna formuläret)."""
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    app = service.mark_sent_manually(job_id, draft.id)
    assert app.channel == Channel.manual
    assert app.status == ApplicationStatus.submitted
    assert app.draft_id == draft.id
    assert job_status(engine, job_id) == JobStatus.submitted
    assert len(applications(engine, job_id)) == 1
    assert form.calls == []  # formuläret fylldes aldrig i


def test_mark_sent_manually_form_job_after_failed_fill(service: ApplyService, engine, form):
    form.crash = RuntimeError("Webbläsaren kraschade")
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.form)
    service.approve_and_submit(job_id, draft.id)
    assert job_status(engine, job_id) == JobStatus.draft_ready

    app = service.mark_sent_manually(job_id, draft.id)
    assert app.channel == Channel.manual
    assert job_status(engine, job_id) == JobStatus.submitted


def test_mark_sent_manually_wrong_status(service: ApplyService, engine):
    job_id, draft = ready_job(service, engine, apply_method=ApplyMethod.email)
    service.dismiss(job_id)
    with pytest.raises(AlreadyHandled, match="avfärdad"):
        service.mark_sent_manually(job_id, draft.id)
    assert job_status(engine, job_id) == JobStatus.dismissed
    assert applications(engine, job_id) == []


def test_mark_sent_manually_draft_of_other_job(service: ApplyService, engine, ai: FakeAI):
    ai.responses[LetterResult] = [letter(), letter()]
    job_a, _ = ready_job(service, engine, apply_method=ApplyMethod.email)
    _, draft_b = ready_job(service, engine, apply_method=ApplyMethod.email)
    for draft_id in (draft_b.id, 999):
        with pytest.raises(LookupError, match="Utkastet finns inte"):
            service.mark_sent_manually(job_a, draft_id)
    assert job_status(engine, job_a) == JobStatus.draft_ready
