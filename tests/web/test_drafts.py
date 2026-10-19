from html import escape

import pytest
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.apply.base import SubmitResult
from jobaplier.apply.letter import LetterResult
from jobaplier.apply.package import mailto_link
from jobaplier.apply.tailor import TailoredCV
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
from jobaplier.profile.cv import cv_file_hash, extract_cv_text

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
        job_overrides = {
            "apply_method": ApplyMethod.form,
            "apply_email": None,
            "apply_form_url": "https://x.se/f",
            **job_overrides,
        }
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
    assert "Öppna formulär" in html
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


def test_email_job_has_no_approve_button(client, add_draft):
    """Mejljobb fylls aldrig i automatiskt; användaren skickar själv från paketsidan."""
    job_id, _ = add_draft(apply_method=ApplyMethod.email, apply_email="jobb@exempel.se")

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "<textarea" in html and "Spara ändringar" in html
    assert "Godkänn och skicka" not in html
    assert "Öppna formulär" not in html
    assert f"/jobs/{job_id}/draft/approve" not in html
    assert 'name="channel"' not in html
    assert "Torrkörning" not in html


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


def test_approve_email_job_is_rejected(client, add_draft, engine):
    job_id, draft_id = add_draft(apply_method=ApplyMethod.email, apply_email="jobb@exempel.se")

    response = client.post(f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id})

    assert response.status_code == 409
    assert "ingen automatisk ansökningsväg" in response.text
    assert status_of(engine, job_id) == JobStatus.draft_ready
    with Session(engine) as session:
        assert session.exec(select(Application)).all() == []


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
    form_job, _ = add_draft()

    assert '<input type="hidden" name="channel" value="form">' in client.get(
        f"/jobs/{form_job}/draft"
    ).text


@pytest.mark.parametrize("posted", ["email", "bogus"])
def test_approve_with_changed_channel_sends_nothing(client, state, add_draft, engine, posted):
    recorder = Recorder(SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting))
    state.service.appliers[ApplyMethod.form] = recorder
    job_id, draft_id = add_draft()  # formulärjobb; en gammal flik visade en annan väg

    response = client.post(
        f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id, "channel": posted}
    )

    assert response.status_code == 409
    assert "Ansökningsvägen har ändrats" in response.text
    assert f'href="/jobs/{job_id}/draft"' in response.text
    assert recorder.calls == 0
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_approve_with_matching_channel_is_sent(client, state, add_draft):
    recorder = Recorder(SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting))
    state.service.appliers[ApplyMethod.form] = recorder
    job_id, draft_id = add_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/approve", data={"draft_id": draft_id, "channel": "form"}
    )

    assert response.status_code == 200
    assert recorder.calls == 1


def test_approve_twice_shows_already_handled(client, state, add_draft):
    recorder = Recorder(SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting))
    state.service.appliers[ApplyMethod.form] = recorder
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
    # Filerna går att ladda ner om formuläret behöver dem igen.
    assert f'href="/jobs/{job_id}/draft/cv.pdf">Ladda ner CV (PDF)</a>' in html
    assert f'href="/jobs/{job_id}/draft/letter.pdf">Ladda ner personligt brev (PDF)</a>' in html
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
    state.service.appliers[ApplyMethod.form] = Recorder(
        SubmitResult(channel=Channel.form, status=ApplicationStatus.failed, notes=["SMTP nere <x>"])
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
    recorder = Recorder(SubmitResult(channel=Channel.form, status=ApplicationStatus.submitting))
    state.service.appliers[ApplyMethod.form] = recorder
    job_id, draft_id = add_draft()
    data = {"draft_id": draft_id, "letter_text": "Ändrad text"}

    first = client.post(f"/jobs/{job_id}/draft/approve", data=data)
    second = client.post(f"/jobs/{job_id}/draft/approve", data=data)

    assert first.status_code == 200
    assert second.status_code == 409
    assert recorder.calls == 1
    assert len(drafts_of(engine, job_id)) == 2


# --- ansökningspaketet ----------------------------------------------------------------------

FALLBACK = (
    "Inget skräddarsytt CV finns för det här utkastet – nedladdningen ger ditt original-CV"
)


@pytest.fixture
def add_email_draft(add_draft):
    def _add(**overrides) -> tuple[int, int]:
        values = {
            "apply_method": ApplyMethod.email,
            "apply_email": "jobb@exempel.se",
            "apply_form_url": None,
            **overrides,
        }
        return add_draft(**values)

    return _add


@pytest.fixture
def add_tailored(engine, paths):
    """Ett skräddarsytt CV (för nuvarande CV-fil) till utkastet."""

    def _add(draft_id: int, warnings=()) -> None:
        tailored = TailoredCV(
            language="sv", headline="", summary="", experience=[], skills=[], education_order=[]
        )
        with session_scope(engine) as session:
            session.add(
                TailoredCVRecord(
                    draft_id=draft_id,
                    cv_hash=cv_file_hash(paths.cv),
                    tailored_json=tailored.model_dump(mode="json"),
                    warnings_json=list(warnings),
                )
            )

    return _add


def test_package_email_job(client, add_email_draft, config):
    config.profile.phone = "070-123 45 67"
    job_id, draft_id = add_email_draft(title="Utvecklare", apply_reference="REF-7")

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Ansökningspaket" in html
    assert 'id="package-to" value="jobb@exempel.se"' in html
    assert 'id="package-subject" value="Ansökan: Utvecklare (ref. REF-7)"' in html
    assert "CV och personligt brev bifogas." in html
    for target in ("package-to", "package-subject", "package-body", "letter-text"):
        assert f'data-copy-target="{target}"' in html
    link = mailto_link(
        "jobb@exempel.se",
        "Ansökan: Utvecklare (ref. REF-7)",
        "Hej!\n\nHär kommer min ansökan till tjänsten Utvecklare. CV och personligt brev "
        "bifogas.\n\nVänliga hälsningar\nTest Testsson\n070-123 45 67",
    )
    assert link.startswith("mailto:jobb@exempel.se?subject=Ans%C3%B6kan%3A%20Utvecklare")
    assert f'href="{escape(link)}"' in html
    assert "Öppna i mejlprogrammet" in html
    assert "Bifoga" in html  # mailto-länken kan inte bifoga filerna
    assert f'href="/jobs/{job_id}/draft/cv.pdf"' in html and "Ladda ner CV (PDF)" in html
    # Brevets PDF och "Jag har skickat" skickar textrutans innehåll (osparade ändringar).
    assert (
        f'form="letter-form" formaction="/jobs/{job_id}/draft/letter.pdf" formmethod="post"'
        in html
    )
    assert "Ladda ner personligt brev (PDF)" in html
    assert f'form="letter-form" formaction="/jobs/{job_id}/draft/sent"' in html
    assert f'name="draft_id" value="{draft_id}"' in html
    assert "Jag har skickat" in html
    assert "Fyll i automatiskt" not in html
    assert '<script src="/static/copy.js" defer></script>' in html


def test_package_email_invalid_address_no_mailto(client, add_email_draft):
    job_id, _ = add_email_draft(apply_email="a@b.se, c@d.se")

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "mailto:" not in html
    assert "Öppna i mejlprogrammet" not in html
    assert 'value="a@b.se, c@d.se"' in html  # adressen visas ändå, att kontrollera
    assert "inte en giltig e-postadress" in html
    assert f'formaction="/jobs/{job_id}/draft/sent"' in html


def test_package_form_job(client, add_draft):
    job_id, _ = add_draft(apply_form_url="https://jobb.example/formular?id=1&x=2")

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Fyll i automatiskt" in html
    assert f'formaction="/jobs/{job_id}/draft/approve"' in html
    assert 'href="https://jobb.example/formular?id=1&amp;x=2" target="_blank"' in html
    assert "Öppna formuläret" in html
    assert f'href="/jobs/{job_id}/draft/cv.pdf"' in html
    assert f'formaction="/jobs/{job_id}/draft/letter.pdf"' in html
    assert 'data-copy-target="letter-text"' in html
    # Fyller användaren i formuläret själv markerar hen det som skickat härifrån.
    sent = f'form="letter-form" formaction="/jobs/{job_id}/draft/sent">Jag har skickat</button>'
    assert sent in html
    assert html.index("Öppna formuläret") < html.index(sent)
    assert "mailto:" not in html


def test_package_form_job_with_unsafe_form_url(client, add_draft):
    job_id, _ = add_draft(apply_form_url="javascript:alert(1)")

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "javascript:" not in html
    assert "Fyll i automatiskt" in html


def test_package_unknown_job(client, add_draft):
    job_id, draft_id = add_draft(
        apply_method=ApplyMethod.unknown,
        apply_form_url=None,
        url="https://arbetsgivare.example/annons/42",
    )

    html = client.get(f"/jobs/{job_id}/draft").text

    assert 'href="https://arbetsgivare.example/annons/42" target="_blank"' in html
    assert "Öppna annonsen" in html
    assert 'data-copy-target="letter-text"' in html
    assert f'formaction="/jobs/{job_id}/draft/sent"' in html
    assert f'name="draft_id" value="{draft_id}"' in html
    assert "Fyll i automatiskt" not in html
    assert f"/jobs/{job_id}/draft/approve" not in html
    assert "mailto:" not in html


def test_package_shows_cv_warnings(client, add_email_draft, add_tailored):
    job_id, draft_id = add_email_draft(unsupported=["Ledde <b>50</b> personer"])
    add_tailored(draft_id, warnings=["Siffran <99> finns inte i ditt CV"])

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Varningar för CV:t" in html
    assert "Siffran &lt;99&gt; finns inte i ditt CV" in html
    assert "Påståenden utan stöd i CV:t" in html
    assert "Ledde &lt;b&gt;50&lt;/b&gt; personer" in html
    assert FALLBACK not in html
    assert f'action="/jobs/{job_id}/draft/tailor"' not in html
    assert 'href="/cv"' in html


FIDELITY = 'Kontrollera i ditt CV: "Acme" hittades inte ordagrant i cv.pdf'
FIDELITY_HTML = "Kontrollera i ditt CV: &#34;Acme&#34; hittades inte ordagrant i cv.pdf"


def test_package_shows_fidelity_warnings_for_tailored_cv(
    client, add_email_draft, add_tailored, cv_warnings
):
    job_id, draft_id = add_email_draft()
    add_tailored(draft_id, warnings=["Kontrollera siffran 9 i: punkt"])
    cv_warnings.append(FIDELITY)

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Varningar för CV:t" in html
    assert FIDELITY_HTML in html
    assert "Kontrollera siffran 9 i: punkt" in html


def test_package_hides_fidelity_warnings_when_original_cv_is_downloaded(
    client, add_email_draft, cv_warnings
):
    job_id, _ = add_email_draft()
    cv_warnings.append(FIDELITY)

    assert FIDELITY_HTML not in client.get(f"/jobs/{job_id}/draft").text


def test_package_download_links_have_no_download_attribute(client, add_email_draft):
    """Utan `download` visar webbläsaren felsidan (svenska) i stället för att spara den."""
    job_id, draft_id = add_email_draft()
    client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    html = client.get(f"/jobs/{job_id}/draft").text  # skickad: båda är länkar

    assert f'<a class="button" href="/jobs/{job_id}/draft/cv.pdf">Ladda ner CV (PDF)</a>' in html
    letter = f'href="/jobs/{job_id}/draft/letter.pdf">Ladda ner personligt brev (PDF)</a>'
    assert f'<a class="button" {letter}' in html
    assert " download>" not in html


def test_package_shows_fallback_without_tailored_cv(client, add_email_draft):
    job_id, _ = add_email_draft()

    html = client.get(f"/jobs/{job_id}/draft").text

    assert FALLBACK in html
    assert f'action="/jobs/{job_id}/draft/tailor"' in html
    assert "Försök igen" in html
    assert "Varningar för CV:t" not in html
    assert "Visa ditt strukturerade CV" in html


def test_package_offers_retry_when_cv_was_replaced(
    client, add_email_draft, add_tailored, paths, make_pdf
):
    job_id, draft_id = add_email_draft()
    add_tailored(draft_id)
    paths.cv.write_bytes(make_pdf("Ett helt nytt CV.").read_bytes())

    html = client.get(f"/jobs/{job_id}/draft").text

    assert "Ditt CV har bytts sedan utkastet skapades" in html
    assert f'action="/jobs/{job_id}/draft/tailor"' in html


INCOMPLETE = "Annonsen kunde inte läsas – brevet och CV:t bygger bara på titel och företag"


def test_package_warns_when_ad_could_not_be_read(client, add_draft):
    job_id, _ = add_draft(apply_method=ApplyMethod.unknown, apply_form_url=None, incomplete=True)

    assert INCOMPLETE in client.get(f"/jobs/{job_id}/draft").text


def test_package_has_no_incomplete_warning_for_complete_ad(client, add_draft):
    job_id, _ = add_draft()

    assert INCOMPLETE not in client.get(f"/jobs/{job_id}/draft").text


def test_copy_js_served(client, add_email_draft):
    response = client.get("/static/copy.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "data-copy-target" in response.text
    assert "navigator.clipboard.writeText" in response.text
    assert "Kopierat!" in response.text


# --- nedladdningar --------------------------------------------------------------------------


def test_download_cv_tailored(client, add_email_draft, cv_pdf, config):
    config.profile.name = "Vidar Mårtensson"
    cv_pdf.tailored = True
    job_id, draft_id = add_email_draft(company="Åhléns & Co AB")

    response = client.get(f"/jobs/{job_id}/draft/cv.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="CV_Vidar_Martensson_Ahlens_Co_AB.pdf"'
    )
    assert response.content == cv_pdf.TAILORED
    rendered_for, out = cv_pdf.calls[-1]
    assert rendered_for == draft_id
    assert not out.parent.exists()  # den tillfälliga katalogen är borttagen


def test_download_cv_fallback_original(client, add_email_draft, cv_pdf, config, paths):
    config.profile.name = "Vidar Mårtensson"
    job_id, _ = add_email_draft(company="Åhléns & Co AB")

    response = client.get(f"/jobs/{job_id}/draft/cv.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="CV_Vidar_Martensson_Ahlens_Co_AB.pdf"'
    )
    assert response.content == paths.cv.read_bytes()
    assert paths.cv.is_file()  # originalet städas aldrig bort


def test_download_cv_name_falls_back_to_cv_document(
    client, add_email_draft, config, cv_document
):
    from jobaplier.profile.cv_document import CVDocument

    config.profile.name = ""
    cv_document.document = CVDocument(name="Jane Doe")
    job_id, _ = add_email_draft(company="Exempel AB")

    disposition = client.get(f"/jobs/{job_id}/draft/cv.pdf").headers["content-disposition"]

    assert "CV_Jane_Doe_Exempel_AB.pdf" in disposition


def test_download_letter(client, add_email_draft, config, tmp_path):
    config.profile.name = "Vidar Mårtensson"
    job_id, _ = add_email_draft(
        company="Åhléns & Co AB", letter="Hej <b>där</b>!\n\nAndra stycket."
    )

    response = client.get(f"/jobs/{job_id}/draft/letter.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="Personligt_brev_Ahlens_Co_AB.pdf"'
    )
    saved = tmp_path / "brev.pdf"
    saved.write_bytes(response.content)
    text = extract_cv_text(saved)
    assert "Vidar Mårtensson" in text
    assert "Hej <b>där</b>!" in text and "Andra stycket." in text


def test_download_cv_missing_cv_404(client, add_email_draft, paths):
    job_id, _ = add_email_draft()
    paths.cv.unlink()

    response = client.get(f"/jobs/{job_id}/draft/cv.pdf")

    assert response.status_code == 404
    assert "CV-filen saknas" in response.text
    assert 'href="/setup"' in response.text


def test_download_render_error_is_500_in_swedish(client, add_email_draft, cv_pdf):
    job_id, _ = add_email_draft()
    cv_pdf.raises = RuntimeError("pango <kraschade>")

    response = client.get(f"/jobs/{job_id}/draft/cv.pdf")

    assert response.status_code == 500
    assert "PDF:en kunde inte skapas" in response.text
    assert "Traceback" not in response.text and "<kraschade>" not in response.text
    assert not cv_pdf.calls[-1][1].parent.exists()


def test_download_letter_render_error_is_500(client, add_email_draft, monkeypatch):
    from jobaplier.web import routes_drafts

    def broken(*args, **kwargs):
        raise OSError("disken är full")

    monkeypatch.setattr(routes_drafts, "render_letter_pdf", broken)
    job_id, _ = add_email_draft()

    response = client.get(f"/jobs/{job_id}/draft/letter.pdf")

    assert response.status_code == 500
    assert "PDF:en kunde inte skapas" in response.text


def test_downloads_for_missing_job_or_draft_are_404(client, add_job):
    job_id = add_job()  # inget utkast

    for name in ("cv.pdf", "letter.pdf"):
        assert client.get(f"/jobs/999/draft/{name}").status_code == 404
        assert client.get(f"/jobs/{job_id}/draft/{name}").status_code == 404


# --- jag har skickat ------------------------------------------------------------------------


def test_mark_sent_redirects(client, add_email_draft, engine):
    job_id, draft_id = add_email_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}/draft?msg=sent"
    assert status_of(engine, job_id) == JobStatus.submitted
    application = application_of(engine, job_id)
    assert application.channel == Channel.email
    assert application.status == ApplicationStatus.submitted
    assert application.draft_id == draft_id

    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert page.text.count("Markerad som skickad") == 1
    # Paketet går fortfarande att ladda ner, men inte att skicka igen.
    assert f'href="/jobs/{job_id}/draft/cv.pdf"' in page.text
    assert f'href="/jobs/{job_id}/draft/letter.pdf"' in page.text
    assert f"/jobs/{job_id}/draft/sent" not in page.text
    assert f'action="/jobs/{job_id}/draft/tailor"' not in page.text
    assert client.get(f"/jobs/{job_id}/draft/cv.pdf").status_code == 200


def test_mark_sent_unknown_method_uses_manual_channel(client, add_draft, engine):
    job_id, draft_id = add_draft(apply_method=ApplyMethod.unknown, apply_form_url=None)

    client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    assert application_of(engine, job_id).channel == Channel.manual
    assert status_of(engine, job_id) == JobStatus.submitted


def test_mark_sent_stale_409(client, add_email_draft, engine):
    """Review Focus 2: en äldre version i en gammal flik markerar ingenting."""
    job_id, old_draft = add_email_draft()
    client.post(f"/jobs/{job_id}/draft", data={"letter_text": "Nyare version"})

    response = client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": old_draft})

    assert response.status_code == 409
    assert "Utkastet har ändrats" in response.text
    assert "En nyare version av utkastet finns – ladda om sidan" in response.text
    assert "Ansökan är redan hanterad" not in response.text
    assert status_of(engine, job_id) == JobStatus.draft_ready
    with Session(engine) as session:
        assert session.exec(select(Application)).all() == []


def test_mark_sent_twice_is_409(client, add_email_draft):
    job_id, draft_id = add_email_draft()
    client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    response = client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    assert response.status_code == 409
    assert "Ansökan är redan hanterad" in response.text


def test_mark_sent_form_job_uses_manual_channel(client, add_draft, engine):
    job_id, draft_id = add_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}/draft?msg=sent"
    assert status_of(engine, job_id) == JobStatus.submitted
    application = application_of(engine, job_id)
    assert application.channel == Channel.manual
    assert application.draft_id == draft_id


def test_mark_sent_foreign_draft_is_404(client, add_email_draft):
    job_id, _ = add_email_draft()
    _, other_draft = add_email_draft()

    assert client.post(
        f"/jobs/{job_id}/draft/sent", data={"draft_id": other_draft}
    ).status_code == 404


# --- försök igen med CV:t -------------------------------------------------------------------


def test_tailor_retry(client, add_email_draft, state):
    job_id, draft_id = add_email_draft()
    assert state.service.tailored_for(draft_id) is None

    response = client.post(f"/jobs/{job_id}/draft/tailor", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}/draft?msg=tailored"
    assert state.service.tailored_for(draft_id) is not None
    page = client.get(response.headers["location"]).text
    assert "Det skräddarsydda CV:t är klart" in page
    assert FALLBACK not in page


def test_tailor_retry_error_is_shown(client, add_email_draft, ai, state):
    job_id, draft_id = add_email_draft()
    ai.responses[TailoredCV] = [AIError("Modellen svarade inte")]

    response = client.post(f"/jobs/{job_id}/draft/tailor")

    assert response.status_code == 502
    assert "Modellen svarade inte" in response.text
    assert FALLBACK in response.text
    assert state.service.tailored_for(draft_id) is None


def test_tailor_retry_on_handled_job_is_409(client, add_email_draft):
    job_id, draft_id = add_email_draft()
    client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    assert client.post(f"/jobs/{job_id}/draft/tailor").status_code == 409


# --- osparade ändringar följer med till PDF:en och "Jag har skickat" -------------------------


def pdf_text(content: bytes, tmp_path) -> str:
    saved = tmp_path / "ut.pdf"
    saved.write_bytes(content)
    return extract_cv_text(saved)


def test_letter_pdf_with_edited_text_renders_it_without_saving(
    client, add_email_draft, engine, tmp_path
):
    job_id, draft_id = add_email_draft(letter="Ledde ett team på 50 personer.")

    response = client.post(
        f"/jobs/{job_id}/draft/letter.pdf",
        data={"draft_id": draft_id, "letter_text": "Byggde API:er\r\ni Python."},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment")
    text = pdf_text(response.content, tmp_path)
    assert "Byggde API:er" in text and "i Python." in text
    assert "50 personer" not in text
    assert [d.letter_text for d in drafts_of(engine, job_id)] == [
        "Ledde ett team på 50 personer."
    ]


def test_single_tab_download_edit_again_then_sent(client, add_email_draft, engine):
    """Ladda ner med text T1, ändra till T2 och tryck Jag har skickat i samma flik: T2
    sparas som en ny version och markeras som skickad (ingen falsk 409)."""
    job_id, draft_id = add_email_draft(letter="Original")

    downloaded = client.post(
        f"/jobs/{job_id}/draft/letter.pdf", data={"draft_id": draft_id, "letter_text": "T1"}
    )
    again = client.post(
        f"/jobs/{job_id}/draft/letter.pdf", data={"draft_id": draft_id, "letter_text": "T2"}
    )
    sent = client.post(
        f"/jobs/{job_id}/draft/sent",
        data={"draft_id": draft_id, "letter_text": "T2"},
        follow_redirects=False,
    )

    assert downloaded.status_code == 200 and again.status_code == 200
    assert sent.status_code == 303
    drafts = drafts_of(engine, job_id)
    assert [d.letter_text for d in drafts] == ["Original", "T2"]
    assert application_of(engine, job_id).draft_id == drafts[-1].id
    assert status_of(engine, job_id) == JobStatus.submitted


def test_letter_pdf_from_stale_tab_renders_posted_text(client, add_email_draft, engine, tmp_path):
    """Nedladdningen sparar inget, så en äldre flik får bara en PDF av sin egen text."""
    job_id, old_draft = add_email_draft()
    client.post(f"/jobs/{job_id}/draft", data={"letter_text": "Nyare version"})

    response = client.post(
        f"/jobs/{job_id}/draft/letter.pdf",
        data={"draft_id": old_draft, "letter_text": "Text i en gammal flik"},
    )

    assert response.status_code == 200
    assert "Text i en gammal flik" in pdf_text(response.content, tmp_path)
    assert len(drafts_of(engine, job_id)) == 2


def test_letter_pdf_post_after_sent_ignores_posted_edits(client, add_email_draft, tmp_path):
    job_id, draft_id = add_email_draft(letter="Det skickade brevet.")
    client.post(f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id})

    response = client.post(
        f"/jobs/{job_id}/draft/letter.pdf",
        data={"draft_id": draft_id, "letter_text": "Något annat"},
    )

    assert response.status_code == 200
    text = pdf_text(response.content, tmp_path)
    assert "Det skickade brevet." in text and "Något annat" not in text


def test_letter_pdf_post_empty_text_is_rejected(client, add_email_draft, engine):
    job_id, draft_id = add_email_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/letter.pdf", data={"draft_id": draft_id, "letter_text": "  "}
    )

    assert response.status_code == 400
    assert "Brevet får inte vara tomt" in response.text
    assert len(drafts_of(engine, job_id)) == 1


def test_letter_pdf_post_foreign_draft_is_404(client, add_email_draft):
    job_id, _ = add_email_draft()
    _, other_draft = add_email_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/letter.pdf", data={"draft_id": other_draft, "letter_text": "x"}
    )

    assert response.status_code == 404


def test_mark_sent_with_edited_text_marks_the_new_version(client, add_email_draft, engine):
    job_id, draft_id = add_email_draft(letter="Ledde ett team på 50 personer.")

    response = client.post(
        f"/jobs/{job_id}/draft/sent",
        data={"draft_id": draft_id, "letter_text": "Utan påståendet."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    drafts = drafts_of(engine, job_id)
    assert [d.letter_text for d in drafts] == ["Ledde ett team på 50 personer.", "Utan påståendet."]
    assert application_of(engine, job_id).draft_id == drafts[-1].id
    assert status_of(engine, job_id) == JobStatus.submitted


def test_mark_sent_with_unchanged_text_adds_no_version(client, add_email_draft, engine):
    job_id, draft_id = add_email_draft(letter="Rad ett\nrad två")

    client.post(
        f"/jobs/{job_id}/draft/sent",
        data={"draft_id": draft_id, "letter_text": "Rad ett\r\nrad två"},
    )

    assert len(drafts_of(engine, job_id)) == 1
    assert application_of(engine, job_id).draft_id == draft_id


def test_mark_sent_stale_tab_with_edited_text_is_409(client, add_email_draft, engine):
    """Review Focus 2 med osparade ändringar: ingen ny version, inget markeras."""
    job_id, old_draft = add_email_draft()
    client.post(f"/jobs/{job_id}/draft", data={"letter_text": "Nyare version"})

    response = client.post(
        f"/jobs/{job_id}/draft/sent",
        data={"draft_id": old_draft, "letter_text": "Ändring i en gammal flik"},
    )

    assert response.status_code == 409
    assert "Utkastet har ändrats" in response.text
    assert "En nyare version av utkastet finns – ladda om sidan" in response.text
    assert "Ansökan är redan hanterad" not in response.text
    assert len(drafts_of(engine, job_id)) == 2
    assert status_of(engine, job_id) == JobStatus.draft_ready
    with Session(engine) as session:
        assert session.exec(select(Application)).all() == []


def test_mark_sent_empty_text_is_rejected(client, add_email_draft, engine):
    job_id, draft_id = add_email_draft()

    response = client.post(
        f"/jobs/{job_id}/draft/sent", data={"draft_id": draft_id, "letter_text": " "}
    )

    assert response.status_code == 400
    assert status_of(engine, job_id) == JobStatus.draft_ready
