from datetime import datetime

import pytest
from sqlmodel import Session, select

from jobaplier.ai import AIError
from jobaplier.apply.letter import LetterResult
from jobaplier.db import session_scope
from jobaplier.models import (
    AICall,
    Application,
    ApplicationStatus,
    ApplyMethod,
    Channel,
    Draft,
    Job,
    JobSource,
    JobStatus,
)
from jobaplier.pipeline.run import RunInProgress


def status_of(engine, job_id: int) -> JobStatus:
    with Session(engine) as session:
        return session.get(Job, job_id).status


# --- listan ---------------------------------------------------------------------


def test_list_shows_above_threshold_only(client, add_job):
    add_job(title="Hög poäng", score=90)
    add_job(title="Precis på gränsen", score=60)
    add_job(title="Under gränsen", score=59)
    add_job(title="Avfärdad", score=95, status=JobStatus.dismissed)

    html = client.get("/").text

    assert "Hög poäng" in html
    assert "Precis på gränsen" in html
    assert "Under gränsen" not in html
    assert "Avfärdad" not in html
    assert html.index("Hög poäng") < html.index("Precis på gränsen")


def test_row_shows_company_sources_reasons_and_method(client, add_job):
    add_job(
        title="Backendutvecklare",
        company="Åkeri AB",
        location="Göteborg",
        sources=("platsbanken", "linkedin"),
        score_reasons="Matchar Python",
    )

    html = client.get("/").text

    assert "Åkeri AB" in html
    assert "Göteborg" in html
    assert "platsbanken" in html and "linkedin" in html
    assert "Matchar Python" in html
    assert "Sök nu" in html


def test_more_shows_below_threshold(client, add_job):
    add_job(title="Hög poäng", score=90)
    add_job(title="Under gränsen", score=40)
    add_job(title="Ny och opoängsatt", score=None, status=JobStatus.new)

    html = client.get("/?more=1").text

    assert "Hög poäng" in html
    assert "Under gränsen" in html
    assert "Ny och opoängsatt" not in html
    assert html.index("Hög poäng") < html.index("Under gränsen")


def test_status_and_source_filters(client, add_job):
    add_job(title="Från Platsbanken", sources=("platsbanken",))
    add_job(title="Från LinkedIn", sources=("linkedin",))
    add_job(title="Länkad", status=JobStatus.link)
    add_job(title="Bortfiltrerad", status=JobStatus.filtered, score=None)

    by_source = client.get("/?source=linkedin").text
    assert "Från LinkedIn" in by_source
    assert "Från Platsbanken" not in by_source

    by_status = client.get("/?status=link").text
    assert "Länkad" in by_status
    assert "Från LinkedIn" not in by_status

    filtered = client.get("/?status=filtered").text
    assert "Bortfiltrerad" in filtered


def test_invalid_status_falls_back_to_default_list(client, add_job):
    add_job(title="Hög poäng")

    response = client.get("/?status=finns-inte")

    assert response.status_code == 200
    assert "Hög poäng" in response.text


def test_no_apply_button_for_unknown(client, add_job):
    """Review Focus 4: ingen Ansök-knapp, men länk till annonsen."""
    unknown = add_job(
        title="Okänd väg",
        apply_method=ApplyMethod.unknown,
        apply_email=None,
        url="https://arbetsformedlingen.example/annons/42",
    )
    email = add_job(title="Mejlväg", apply_method=ApplyMethod.email)

    html = client.get("/").text

    assert f"/jobs/{unknown}/apply" not in html
    assert f"/jobs/{email}/apply" in html
    assert "https://arbetsformedlingen.example/annons/42" in html
    assert f"/jobs/{unknown}/link" in html
    assert f"/jobs/{unknown}/dismiss" in html


def test_apply_rejected_for_unknown_method(client, add_job, engine):
    job_id = add_job(apply_method=ApplyMethod.unknown, apply_email=None)

    response = client.post(f"/jobs/{job_id}/apply", headers={"HX-Request": "true"})

    assert "HX-Redirect" not in response.headers
    assert "ingen automatisk ansökningsväg" in response.text
    assert status_of(engine, job_id) == JobStatus.scored


def test_unsafe_url_scheme_is_not_linked(client, add_job):
    job_id = add_job(url="javascript:alert(1)")

    assert "javascript:" not in client.get("/").text
    assert "javascript:" not in client.get(f"/jobs/{job_id}").text


# --- åtgärder ---------------------------------------------------------------------


def test_link_action_updates_row(client, add_job, engine):
    job_id = add_job(title="Länka mig")

    response = client.post(f"/jobs/{job_id}/link", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Länka mig" in response.text
    assert "<html" not in response.text  # bara radfragmentet
    assert "ansök via länk" in response.text
    assert f"/jobs/{job_id}/dismiss" not in response.text
    assert status_of(engine, job_id) == JobStatus.link


def test_dismiss_action(client, add_job, engine):
    job_id = add_job(title="Avfärda mig")

    response = client.post(f"/jobs/{job_id}/dismiss", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "avfärdad" in response.text
    assert status_of(engine, job_id) == JobStatus.dismissed
    assert "Avfärda mig" not in client.get("/").text


def test_action_without_htmx_redirects_to_list(client, add_job, engine):
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/dismiss", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/")
    assert status_of(engine, job_id) == JobStatus.dismissed


def test_already_handled_rerenders_row_with_message(client, add_job):
    job_id = add_job(status=JobStatus.dismissed)

    response = client.post(f"/jobs/{job_id}/link", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Åtgärden går inte att utföra" in response.text


def test_apply_redirects_to_draft(client, add_job, engine):
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/apply", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/jobs/{job_id}/draft"
    assert status_of(engine, job_id) == JobStatus.draft_ready


def test_apply_without_htmx_redirects_to_draft(client, add_job):
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/apply", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}/draft"


def test_apply_ai_error_shows_message_in_row(client, state, add_job, engine):
    state.service.ai.responses[LetterResult] = [AIError("Modellen svarade inte")]
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/apply", headers={"HX-Request": "true"})

    assert "HX-Redirect" not in response.headers
    assert "Kunde inte skriva brevet: Modellen svarade inte" in response.text
    assert status_of(engine, job_id) == JobStatus.scored


def test_unknown_job_is_404_in_swedish(client):
    for response in (
        client.get("/jobs/9999"),
        client.post("/jobs/9999/link"),
        client.post("/jobs/9999/apply"),
    ):
        assert response.status_code == 404
        assert "finns inte" in response.text


# --- ansökningsflödet: ingångar i listan och på detaljsidan --------------------------------


def _section(html: str, start: str, end: str) -> str:
    return html[html.index(start) : html.index(end, html.index(start))]


def _row(html: str, job_id: int) -> str:
    start = html.index(f'id="job-{job_id}"')
    return html[start : html.index("</article>", start)]


def _add_application(engine, job_id: int, channel: Channel, status: ApplicationStatus) -> None:
    with session_scope(engine) as session:
        session.add(Draft(job_id=job_id, version=1, letter_text="Hej!", language="sv"))
        session.flush()
        session.add(Application(job_id=job_id, channel=channel, status=status, error="Fel"))


def test_link_job_offers_mark_applied_in_row_and_detail(client, add_job):
    job_id = add_job(title="Länkjobb", status=JobStatus.link, apply_method=ApplyMethod.unknown)

    row = _row(client.get("/?status=link").text, job_id)
    for html in (row, client.get(f"/jobs/{job_id}").text):
        assert f'action="/jobs/{job_id}/mark-submitted"' in html
        assert "Markera som ansökt själv" in html


def test_mark_applied_from_row_updates_row_and_creates_manual_application(
    client, add_job, engine
):
    job_id = add_job(title="Länkjobb", status=JobStatus.link, apply_method=ApplyMethod.unknown)

    response = client.post(f"/jobs/{job_id}/mark-submitted", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text  # bara radfragmentet
    assert "skickad" in response.text
    assert "Markera som ansökt själv" not in response.text
    assert status_of(engine, job_id) == JobStatus.submitted
    with Session(engine) as session:
        (app,) = session.exec(select(Application).where(Application.job_id == job_id)).all()
    assert app.channel == Channel.manual
    assert app.status == ApplicationStatus.submitted


def test_mark_applied_twice_from_row_shows_message(client, add_job):
    job_id = add_job(status=JobStatus.submitted)

    response = client.post(f"/jobs/{job_id}/mark-submitted", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Åtgärden går inte att utföra" in response.text


def test_mark_applied_without_htmx_shows_outcome_page(client, add_job, engine):
    job_id = add_job(status=JobStatus.link, apply_method=ApplyMethod.unknown)

    html = client.post(f"/jobs/{job_id}/mark-submitted").text

    assert "Markerad som skickad" in html
    assert status_of(engine, job_id) == JobStatus.submitted


def test_draft_ready_links_to_draft(client, add_job, engine):
    job_id = add_job(status=JobStatus.draft_ready)

    for html in (_row(client.get("/").text, job_id), client.get(f"/jobs/{job_id}").text):
        assert f'href="/jobs/{job_id}/draft"' in html
        assert "Granska utkast" in html


def test_failed_job_offers_draft_and_retry(client, add_job, engine):
    job_id = add_job(status=JobStatus.failed)
    _add_application(engine, job_id, Channel.email, ApplicationStatus.failed)

    for html in (_row(client.get("/").text, job_id), client.get(f"/jobs/{job_id}").text):
        assert f'href="/jobs/{job_id}/draft"' in html
        assert "Visa utkast" in html
        assert f'action="/jobs/{job_id}/retry"' in html
        assert "Försök igen" in html


def test_form_submitting_offers_continue_and_mark_sent(client, add_job, engine):
    job_id = add_job(status=JobStatus.submitting, apply_method=ApplyMethod.form)
    _add_application(engine, job_id, Channel.form, ApplicationStatus.submitting)

    for html in (_row(client.get("/").text, job_id), client.get(f"/jobs/{job_id}").text):
        assert f'href="/jobs/{job_id}/draft"' in html
        assert "Fortsätt" in html
        assert f'action="/jobs/{job_id}/mark-submitted"' in html
        assert "Jag har skickat" in html


def test_email_submitting_has_no_mark_sent(client, add_job, engine):
    job_id = add_job(status=JobStatus.submitting)
    _add_application(engine, job_id, Channel.email, ApplicationStatus.submitting)

    for html in (_row(client.get("/").text, job_id), client.get(f"/jobs/{job_id}").text):
        assert f'href="/jobs/{job_id}/draft"' in html
        assert "mark-submitted" not in html


def test_default_list_shows_jobs_in_apply_flow_first(client, add_job, engine):
    add_job(title="Poängsatt jobb", score=90)
    add_job(title="Utkast under tröskeln", status=JobStatus.draft_ready, score=10)
    add_job(title="Skickas nu", status=JobStatus.submitting, apply_method=ApplyMethod.form)
    add_job(title="Misslyckat jobb", status=JobStatus.failed, score=None)
    add_job(title="Redan skickat", status=JobStatus.submitted)
    add_job(title="Länkat jobb", status=JobStatus.link)
    add_job(title="Utkast från LinkedIn", status=JobStatus.draft_ready, sources=("linkedin",))

    html = client.get("/").text

    ongoing = _section(html, "Pågående ansökningar", "<h1>Jobb")
    for title in ("Utkast under tröskeln", "Skickas nu", "Misslyckat jobb", "Utkast från LinkedIn"):
        assert title in ongoing
    assert "Poängsatt jobb" not in ongoing
    assert "Poängsatt jobb" in html
    assert "Redan skickat" not in html
    assert "Länkat jobb" not in html

    by_source = client.get("/?source=linkedin").text
    assert "Utkast från LinkedIn" in by_source
    assert "Utkast under tröskeln" not in by_source

    by_status = client.get("/?status=dismissed").text
    assert "Pågående ansökningar" not in by_status


def test_no_ongoing_section_without_jobs_in_apply_flow(client, add_job):
    add_job(title="Poängsatt jobb")

    assert "Pågående ansökningar" not in client.get("/").text


# --- Sök nu ---------------------------------------------------------------------------


def test_run_now_starts_a_run(client, run_now):
    response = client.post("/run", follow_redirects=True)

    assert run_now.calls == 1
    assert "Sökning startad" in response.text


def test_run_now_in_progress_message(client, run_now):
    run_now.raises = RunInProgress("pågår")

    response = client.post("/run", follow_redirects=True)

    assert "En sökning pågår redan" in response.text


def test_flash_message_is_whitelisted(client):
    html = client.get("/?msg=<script>alert(1)</script>").text

    assert "<script>alert(1)" not in html


# --- bannrar --------------------------------------------------------------------------


def test_source_error_banner(client, add_run):
    add_run(
        started_at=datetime(2026, 10, 19, 8, 0),
        errors_json={"platsbanken": "Timeout <b>efter 30 s</b>"},
    )

    html = client.get("/").text

    assert "Timeout &lt;b&gt;efter 30 s&lt;/b&gt;" in html
    assert "platsbanken" in html


def test_banner_reads_latest_run_only(client, add_run):
    add_run(started_at=datetime(2026, 10, 18, 8, 0), errors_json={"platsbanken": "Gammalt fel"})
    add_run(started_at=datetime(2026, 10, 19, 8, 0), errors_json={})

    assert "Gammalt fel" not in client.get("/").text


def test_no_banner_without_runs(client):
    html = client.get("/").text

    assert "banner" not in html


def test_budget_banner_api(client, config, engine):
    config.ai.backend = "api"
    config.ai_monthly_budget_usd = 5.0
    with session_scope(engine) as session:
        session.add(AICall(model="claude-haiku-5-5", purpose="score", cost_usd=5.5))

    html = client.get("/").text

    assert "Månadstaket" in html


def test_no_budget_banner_when_under_cap(client, config):
    config.ai.backend = "api"

    assert "Månadstaket" not in client.get("/").text


def test_no_budget_banner_for_claude_code_backend(client, config, engine):
    config.ai_monthly_budget_usd = 1.0
    with session_scope(engine) as session:
        session.add(AICall(model="claude-haiku-5-5", purpose="score", cost_usd=50.0))

    assert "Månadstaket" not in client.get("/").text


def test_usage_limit_banner_claude_code(client, config, add_run):
    config.ai.backend = "claude_code"
    add_run(
        started_at=datetime(2026, 10, 19, 8, 0),
        errors_json={"budget": "Prenumerationens användningsgräns är nådd"},
    )

    html = client.get("/").text

    assert "Prenumerationens användningsgräns är nådd" in html


# --- detalj ----------------------------------------------------------------------------


def test_detail_shows_filter_reason(client, add_job):
    job_id = add_job(
        title="Bortfiltrerat jobb",
        status=JobStatus.filtered,
        filter_reason="Utesluten nyckelord: säljare",
        score=None,
        score_reasons=None,
    )

    html = client.get(f"/jobs/{job_id}").text

    assert "Bortfiltrerat jobb" in html
    assert "Utesluten nyckelord: säljare" in html


def test_detail_shows_ad_sources_and_incomplete(client, add_job):
    job_id = add_job(
        title="Fullt jobb",
        description="Rad ett\nRad <b>två</b>",
        incomplete=True,
        sources=("platsbanken", "linkedin"),
        score=77,
        score_reasons="Passar bra",
    )

    html = client.get(f"/jobs/{job_id}").text

    assert "Rad ett\nRad &lt;b&gt;två&lt;/b&gt;" in html
    assert "ofullständig" in html
    assert "https://platsbanken.example/" in html
    assert "https://linkedin.example/" in html
    assert "77" in html
    assert "Passar bra" in html


# --- skydd mot trasiga och fientliga annonsdata ------------------------------------------


def test_malformed_urls_do_not_break_pages(client, add_job, engine):
    job_id = add_job(title="Trasig länk", url="http://[abc")
    with session_scope(engine) as session:
        session.add(
            JobSource(job_id=job_id, source="trasig", external_id="x", url="https://exa[mple.com/x")
        )

    for path in ("/", f"/jobs/{job_id}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Trasig länk" in response.text
        assert "http://[abc" not in response.text
        assert "exa[mple.com" not in response.text


def test_script_in_title_and_company_is_escaped(client, add_job):
    job_id = add_job(title="<script>alert(1)</script>", company="<script>alert(2)</script>")

    for path in ("/", f"/jobs/{job_id}"):
        html = client.get(path).text
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


def test_javascript_urls_in_source_and_form_are_not_linked(client, add_job, engine):
    job_id = add_job(
        apply_method=ApplyMethod.form,
        apply_form_url="javascript:alert(3)",
        url="https://example.se/ok",
    )
    with session_scope(engine) as session:
        session.add(
            JobSource(job_id=job_id, source="ond", external_id="y", url="javascript:alert(4)")
        )

    html = client.get(f"/jobs/{job_id}").text

    assert "javascript:" not in html
    assert "ond" in html  # källnamnet visas, men utan länk


# --- CSRF och DNS-rebinding ------------------------------------------------------------


def test_cross_origin_post_is_forbidden(client, add_job, engine, run_now):
    job_id = add_job()
    evil = {"Origin": "https://evil.example"}

    assert client.post(f"/jobs/{job_id}/dismiss", headers=evil).status_code == 403
    assert client.post(f"/jobs/{job_id}/apply", headers=evil).status_code == 403
    response = client.post("/run", headers=evil)

    assert response.status_code == 403
    assert "Förbjudet" in response.text
    assert run_now.calls == 0
    assert status_of(engine, job_id) == JobStatus.scored


def test_cross_site_referer_without_origin_is_forbidden(client, add_job, engine):
    job_id = add_job()

    response = client.post(
        f"/jobs/{job_id}/dismiss", headers={"Referer": "https://evil.example/page"}
    )

    assert response.status_code == 403
    assert status_of(engine, job_id) == JobStatus.scored


def test_same_origin_and_headerless_posts_are_allowed(client, add_job, engine):
    first, second = add_job(), add_job()

    local = client.post(
        f"/jobs/{first}/dismiss",
        headers={"Origin": "http://127.0.0.1:8000"},
        follow_redirects=False,
    )
    headerless = client.post(f"/jobs/{second}/dismiss", follow_redirects=False)

    assert local.status_code == 303
    assert headerless.status_code == 303
    assert status_of(engine, first) == JobStatus.dismissed
    assert status_of(engine, second) == JobStatus.dismissed


def test_bad_host_is_forbidden_for_get_and_post(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/run", headers={"Host": "evil.example:8000"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost:8000"}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://localhost:3000"},
        {"Origin": "http://127.0.0.1:3000"},
        {"Origin": "https://127.0.0.1:8000"},
        {"Origin": "http://127.0.0.1"},
        {"Origin": "null"},
        {"Referer": "http://localhost:3000/sida"},
        {"Referer": "http://127.0.0.1:5173/"},
    ],
)
def test_other_local_port_or_scheme_is_forbidden(client, add_job, engine, headers):
    """En sida på en annan port på localhost är en annan webbplats (annat origin)."""
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/dismiss", headers=headers)

    assert response.status_code == 403
    assert status_of(engine, job_id) == JobStatus.scored


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://localhost:8000"},
        {"Origin": "http://LOCALHOST:8000"},
        {"Referer": "http://127.0.0.1:8000/jobs/1/draft"},
        {"Origin": "http://testserver"},
    ],
)
def test_exact_dashboard_origins_are_allowed(client, add_job, headers):
    job_id = add_job()

    response = client.post(f"/jobs/{job_id}/dismiss", headers=headers, follow_redirects=False)

    assert response.status_code == 303


def test_host_with_other_port_is_forbidden(client):
    assert client.get("/", headers={"Host": "127.0.0.1:3000"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:8080"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:abc"}).status_code == 403


def test_local_origins_follow_the_bind_port():
    from jobaplier.config import DASHBOARD_PORT
    from jobaplier.web.app import local_origins

    assert DASHBOARD_PORT == 8000
    assert local_origins() == {"http://127.0.0.1:8000", "http://localhost:8000"}
    assert local_origins(9000) == {"http://127.0.0.1:9000", "http://localhost:9000"}


def test_default_allowed_hosts_exclude_testserver(state):
    from fastapi.testclient import TestClient

    from jobaplier.web.app import create_app

    assert TestClient(create_app(state)).get("/").status_code == 403


def test_htmx_is_served_locally_not_from_a_cdn(client):
    """htmx körs med dashboardens fulla behörighet (skicka ansökningar, ändra
    inställningar) och får därför inte laddas från en extern CDN."""
    html = client.get("/").text

    assert '<script src="/static/htmx.min.js" defer></script>' in html
    assert "unpkg.com" not in html
    script = client.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert script.text.startswith('var htmx=function(){"use strict";')
    assert 'version:"2.0.4"' in script.text
