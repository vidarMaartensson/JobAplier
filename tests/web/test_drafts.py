import pytest
from sqlmodel import Session, select

from jobaplier.apply.base import SubmitResult
from jobaplier.apply.letter import LetterResult
from jobaplier.db import session_scope
from jobaplier.models import (
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobStatus,
)

LETTER = "Hej och välkommen. Jag söker tjänsten som utvecklare hos er."


def status_of(engine, job_id: int) -> JobStatus:
    with Session(engine) as session:
        return session.get(Job, job_id).status


def drafts_of(engine, job_id: int) -> list[Draft]:
    with Session(engine) as session:
        return list(
            session.exec(select(Draft).where(Draft.job_id == job_id).order_by(Draft.version))
        )


def application_of(engine, job_id: int) -> Application:
    with Session(engine) as session:
        return session.exec(select(Application).where(Application.job_id == job_id)).one()


class Recorder:
    """Applier-dubblett som returnerar ett färdigt resultat och räknar anrop."""

    def __init__(self, result: SubmitResult) -> None:
        self.result = result
        self.calls = 0

    def submit(self, job, draft) -> SubmitResult:
        self.calls += 1
        return self.result


@pytest.fixture
def add_draft(engine, add_job):
    """Ett jobb med status `draft_ready` och ett utkast; returnerar (job_id, draft_id)."""

    def _add(letter=LETTER, unsupported=(), **job_overrides) -> tuple[int, int]:
        job_id = add_job(status=JobStatus.draft_ready, **job_overrides)
        with session_scope(engine) as session:
            draft = Draft(
                job_id=job_id,
                version=1,
                letter_text=letter,
                language="sv",
                unsupported_claims_json=list(unsupported),
            )
            session.add(draft)
            session.flush()
            return job_id, draft.id

    return _add


# --- sidan ---------------------------------------------------------------------------


def test_draft_page_shows_letter_warnings_and_wordcount(client, add_draft):
    job_id, _ = add_draft(
        letter="Ett två tre <script>alert(1)</script> fyra",
        unsupported=["Ledde ett team <b>på 50</b> personer"],
        title="Backendutvecklare",
        description="Annonstext med <i>html</i> i",
    )

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Backendutvecklare" in html
    assert "Annonstext med &lt;i&gt;html&lt;/i&gt; i" in html
    assert "Ett två tre &lt;script&gt;alert(1)&lt;/script&gt; fyra" in html
    assert "<script>alert(1)" not in html
    assert "<textarea" in html
    assert "5 ord" in html
    assert "Påståenden utan stöd i CV:t" in html
    assert "Ledde ett team &lt;b&gt;på 50&lt;/b&gt; personer" in html
    assert "Godkänn och skicka" in html
    assert 'name="instruction"' in html


def test_no_warning_block_without_unsupported_claims(client, add_draft):
    job_id, _ = add_draft(unsupported=[])

    assert "Påståenden utan stöd" not in client.get(f"/jobs/{job_id}/draft").text


def test_form_job_shows_open_form_button(client, add_draft):
    job_id, _ = add_draft(
        apply_method=ApplyMethod.form, apply_email=None, apply_form_url="https://x.se/f"
    )

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Öppna formulär" in html
    assert "Godkänn och skicka" not in html


def test_dry_run_banner(client, add_draft, config):
    job_id, _ = add_draft()
    assert "Torrkörning är på – mejlet skickas till dig själv" in client.get(
        f"/jobs/{job_id}/draft"
    ).text

    config.apply.dry_run = False
    assert "Torrkörning är på" not in client.get(f"/jobs/{job_id}/draft").text


def test_draft_page_for_missing_job_or_draft_is_404(client, add_job):
    assert client.get("/jobs/999/draft").status_code == 404
    job_id = add_job()  # poängsatt, inget utkast ännu
    response = client.get(f"/jobs/{job_id}/draft")
    assert response.status_code == 404
    assert "Det finns inget utkast" in response.text


# --- spara och ny version ------------------------------------------------------------------


def test_save_edit_creates_new_version(client, add_draft, engine):
    job_id, _ = add_draft()

    response = client.post(
        f"/jobs/{job_id}/draft",
        data={"letter_text": "Ny text\r\nrad två"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/jobs/{job_id}/draft")
    drafts = drafts_of(engine, job_id)
    assert [d.version for d in drafts] == [1, 2]
    assert drafts[-1].letter_text == "Ny text\nrad två"  # webbläsarens \r\n normaliseras
    assert "Sparat" in client.get(response.headers["location"]).text


def test_save_empty_text_shows_error(client, add_draft, engine):
    job_id, _ = add_draft()

    response = client.post(f"/jobs/{job_id}/draft", data={"letter_text": "  "})

    assert response.status_code == 400
    assert "Brevet får inte vara tomt" in response.text
    assert len(drafts_of(engine, job_id)) == 1


def test_save_edit_on_handled_job_is_rejected(client, add_job):
    job_id = add_job(status=JobStatus.submitted)

    response = client.post(f"/jobs/{job_id}/draft", data={"letter_text": "x"})

    assert response.status_code == 409
    assert "Ansökan är redan hanterad" in response.text


def test_regenerate_passes_instruction(client, add_draft, ai, engine):
    job_id, _ = add_draft()
    ai.responses[LetterResult][:] = [
        LetterResult(language="sv", letter="Kortare brev.", unsupported_claims=[])
    ]

    response = client.post(
        f"/jobs/{job_id}/draft/regenerate", data={"instruction": "Gör det kortare"}
    )

    assert response.status_code == 200
    assert "Gör det kortare" in ai.calls[-1]["prompt"]
    drafts = drafts_of(engine, job_id)
    assert [d.version for d in drafts] == [1, 2]
    assert drafts[-1].letter_text == "Kortare brev."
    assert drafts[-1].instruction == "Gör det kortare"
    assert "Kortare brev." in response.text


def test_regenerate_without_instruction_is_rejected(client, add_draft, ai, engine):
    job_id, _ = add_draft()

    response = client.post(f"/jobs/{job_id}/draft/regenerate", data={"instruction": "  "})

    assert response.status_code == 400
    assert "instruktion" in response.text
    assert ai.calls == []
    assert len(drafts_of(engine, job_id)) == 1


def test_regenerate_ai_error_is_shown(client, add_draft, ai, engine):
    from jobaplier.ai import AIError

    job_id, _ = add_draft()
    ai.responses[LetterResult][:] = [AIError("Modellen svarade inte")]

    response = client.post(f"/jobs/{job_id}/draft/regenerate", data={"instruction": "Kortare"})

    assert response.status_code == 502
    assert "Modellen svarade inte" in response.text
    assert len(drafts_of(engine, job_id)) == 1


# --- godkänn och skicka -----------------------------------------------------------------


def test_approve_email_shows_submitted(client, add_draft, engine):
    job_id, draft_id = add_draft(apply_email="rekrytering@exempel.se")

    response = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id})

    assert response.status_code == 200
    assert "Ansökan skickad" in response.text
    assert "rekrytering@exempel.se" in response.text
    assert "Torrkörning" not in response.text
    assert status_of(engine, job_id) == JobStatus.submitted
    assert application_of(engine, job_id).status == ApplicationStatus.submitted


def test_approve_dry_run_says_mail_went_to_self(client, state, add_draft, config, engine):
    state.service.appliers[ApplyMethod.email] = Recorder(
        SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted, dry_run=True)
    )
    job_id, draft_id = add_draft(apply_email="rekrytering@exempel.se")

    html = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id}).text

    assert "Torrkörning – mejlet skickades till dig själv. Jobbet ligger kvar som utkast." in html
    assert "<h1>Torrkörning klar</h1>" in html
    assert config.profile.email in html
    assert f'href="/jobs/{job_id}/draft"' in html
    assert status_of(engine, job_id) == JobStatus.draft_ready
    # Utkastsidan visar redigeraren igen, så att jobbet kan skickas på riktigt.
    assert "Godkänn och skicka" in client.get(f"/jobs/{job_id}/draft").text


def test_approve_saves_unsaved_textarea_edits_first(client, add_draft, engine):
    job_id, draft_id = add_draft()

    client.post(
        f"/jobs/{job_id}/draft/approve",
        data={"draft_id": draft_id, "letter_text": "Ändrad text\r\nsista raden"},
    )

    drafts = drafts_of(engine, job_id)
    assert [d.letter_text for d in drafts] == [LETTER, "Ändrad text\nsista raden"]
    assert application_of(engine, job_id).draft_id == drafts[-1].id


def test_approve_unchanged_text_does_not_add_a_version(client, add_draft, engine):
    job_id, draft_id = add_draft(letter="Rad ett\nrad två")

    client.post(
        f"/jobs/{job_id}/draft/approve",
        data={"draft_id": draft_id, "letter_text": "Rad ett\r\nrad två"},
    )

    assert len(drafts_of(engine, job_id)) == 1


def test_approve_form_posts_the_channel_the_user_saw(client, add_draft):
    email_job, _ = add_draft()
    form_job, _ = add_draft(apply_method=ApplyMethod.form, apply_form_url="https://x.se/f")

    assert '<input type="hidden" name="channel" value="email">' in client.get(
        f"/jobs/{email_job}/draft"
    ).text
    assert '<input type="hidden" name="channel" value="form">' in client.get(
        f"/jobs/{form_job}/draft"
    ).text


@pytest.mark.parametrize("posted", ["form", "bogus"])
def test_approve_with_changed_channel_sends_nothing(client, state, add_draft, engine, posted):
    recorder = Recorder(SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted))
    state.service.appliers[ApplyMethod.email] = recorder
    job_id, draft_id = add_draft()  # mejljobb; sidan visade formulär

    response = client.post(
        f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id, "channel": posted}
    )

    assert response.status_code == 409
    assert "Ansökningsvägen har ändrats" in response.text
    assert f'href="/jobs/{job_id}/draft"' in response.text
    assert recorder.calls == 0
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_approve_with_matching_channel_is_sent(client, state, add_draft):
    recorder = Recorder(SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted))
    state.service.appliers[ApplyMethod.email] = recorder
    job_id, draft_id = add_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id, "channel": "email"}
    )

    assert response.status_code == 200
    assert recorder.calls == 1


def test_approve_twice_shows_already_handled(client, state, add_draft):
    recorder = Recorder(SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted))
    state.service.appliers[ApplyMethod.email] = recorder
    job_id, draft_id = add_draft()

    client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id})
    second = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id})

    assert second.status_code == 409
    assert "Ansökan är redan hanterad" in second.text
    assert recorder.calls == 1


def test_approve_unknown_draft_is_404(client, add_draft):
    job_id, _ = add_draft()

    assert client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": 999}).status_code == 404


def test_approve_without_auto_method_is_rejected(client, add_draft, engine):
    job_id, draft_id = add_draft(apply_method=ApplyMethod.unknown, apply_email=None)

    response = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id})

    assert response.status_code == 409
    assert "ingen automatisk ansökningsväg" in response.text
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_form_approve_shows_unanswered_and_mark_button(client, state, add_draft, engine):
    state.service.appliers[ApplyMethod.form] = Recorder(
        SubmitResult(
            channel=Channel.form,
            status=ApplicationStatus.submitting,
            notes=["Körkort <b>B</b>?", "Löneanspråk"],
        )
    )
    job_id, draft_id = add_draft(
        apply_method=ApplyMethod.form, apply_email=None, apply_form_url="https://x.se/f"
    )

    html = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id}).text

    assert "Formuläret är öppnat i webbläsaren." in html
    assert "Obesvarade frågor: Körkort &lt;b&gt;B&lt;/b&gt;?, Löneanspråk" in html
    assert "Klicka Skicka där och sedan här." in html
    assert f'action="/jobs/{job_id}/mark-submitted"' in html
    assert status_of(engine, job_id) == JobStatus.submitting

    marked = client.post(f"/jobs/{job_id}/mark-submitted")

    assert marked.status_code == 200
    assert "markerad som skickad" in marked.text.lower()
    assert status_of(engine, job_id) == JobStatus.submitted
    assert application_of(engine, job_id).status == ApplicationStatus.submitted


def test_form_approve_without_questions(client, state, add_draft):
    state.service.appliers[ApplyMethod.form] = Recorder(
        SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting)
    )
    job_id, draft_id = add_draft(apply_method=ApplyMethod.form, apply_email=None)

    html = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id}).text

    assert "Formuläret är öppnat i webbläsaren." in html
    assert "Obesvarade frågor" not in html
    assert f'action="/jobs/{job_id}/mark-submitted"' in html


def test_form_crash_returns_to_draft_without_retry_button(client, state, add_draft, engine):
    class Crashing:
        def submit(self, job, draft):
            raise RuntimeError("Webbläsaren dog")

    state.service.appliers[ApplyMethod.form] = Crashing()
    job_id, draft_id = add_draft(apply_method=ApplyMethod.form, apply_email=None)

    html = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id}).text

    assert "Webbläsaren dog" in html
    assert f'action="/jobs/{job_id}/retry"' not in html
    assert f'href="/jobs/{job_id}/draft"' in html
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_failed_shows_retry(client, state, add_draft, engine):
    state.service.appliers[ApplyMethod.email] = Recorder(
        SubmitResult(channel=Channel.email, status=ApplicationStatus.failed, notes=["SMTP nere <x>"])
    )
    job_id, draft_id = add_draft()

    html = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id}).text

    assert "SMTP nere &lt;x&gt;" in html
    assert f'action="/jobs/{job_id}/retry"' in html
    assert status_of(engine, job_id) == JobStatus.failed

    # Utkastsidan för ett misslyckat jobb visar också felet och knappen.
    page = client.get(f"/jobs/{job_id}/draft").text
    assert "SMTP nere &lt;x&gt;" in page
    assert f'action="/jobs/{job_id}/retry"' in page

    retried = client.post(f"/jobs/{job_id}/retry", follow_redirects=False)

    assert retried.status_code == 303
    assert retried.headers["location"] == f"/jobs/{job_id}/draft"
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_retry_on_non_failed_job_is_rejected(client, add_draft):
    job_id, _ = add_draft()

    response = client.post(f"/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert "Ansökan är redan hanterad" in response.text


def test_mark_submitted_on_unknown_job_is_404(client):
    assert client.post("/jobs/999/mark-submitted").status_code == 404


def test_double_click_with_edited_text_sends_once_and_adds_one_version(
    client, state, add_draft, engine
):
    recorder = Recorder(SubmitResult(channel=Channel.email, status=ApplicationStatus.submitted))
    state.service.appliers[ApplyMethod.email] = recorder
    job_id, draft_id = add_draft()
    data = {"draft_id": draft_id, "letter_text": "Ändrad text"}

    first = client.post(f"/jobs/{job_id}/draft/approve", data=data)
    second = client.post(f"/jobs/{job_id}/draft/approve", data=data)

    assert first.status_code == 200
    assert second.status_code == 409
    assert recorder.calls == 1
    assert len(drafts_of(engine, job_id)) == 2
