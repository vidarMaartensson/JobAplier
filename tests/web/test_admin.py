from datetime import datetime

import pytest
from jobaplier.config import CareerSite, Config, load_config
from jobaplier.db import session_scope
from jobaplier.models import AICall, Application, ApplicationStatus, Channel
from jobaplier.profile.cv_document import CVDocument, Education, Experience, OtherSection
from jobaplier.web.setup import compute_setup_missing

# --- ansökningar ---------------------------------------------------------------------------


def test_applications_list(client, add_job, engine):
    sent = add_job(title="Skickad tjänst", company="Åkeri AB")
    failed = add_job(title="Misslyckad tjänst")
    with session_scope(engine) as session:
        session.add(
            Application(
                job_id=sent,
                channel=Channel.email,
                status=ApplicationStatus.submitted,
                receipt="msg-123",
                dry_run=True,
                submitted_at=datetime(2026, 10, 19, 10, 30),  # 12:30 lokal tid (CEST)
            )
        )
        session.add(
            Application(
                job_id=failed,
                channel=Channel.form,
                status=ApplicationStatus.failed,
                error="Formuläret var <trasigt>",
                dry_run=False,
            )
        )

    html = client.get("/applications").text

    assert "Skickad tjänst" in html and "Åkeri AB" in html
    assert "2026-10-19 12:30" in html
    assert "Mejl" in html and "Formulär" in html
    assert "skickad" in html and "misslyckad" in html
    assert "msg-123" in html
    assert "Formuläret var &lt;trasigt&gt;" in html
    assert f'href="/jobs/{sent}"' in html
    assert "<th>Torrkörning</th>" in html  # en gammal torrkörning finns


def test_applications_hide_dry_run_column_without_dry_runs(client, add_job, engine):
    job_id = add_job(title="Skickad tjänst")
    with session_scope(engine) as session:
        session.add(
            Application(job_id=job_id, channel=Channel.manual, status=ApplicationStatus.submitted)
        )

    html = client.get("/applications").text

    assert "Skickad tjänst" in html
    assert "Torrkörning" not in html
    assert "<td>Nej</td>" not in html


def test_applications_empty(client):
    html = client.get("/applications").text

    assert "Inga ansökningar ännu" in html
    assert "När du markerat en ansökan som skickad dyker den upp här" in html
    assert "Godkänn ett utkast" not in html


# --- körningar ---------------------------------------------------------------------------


def test_runs_page_lists_stats_and_errors(client, add_run):
    add_run(
        trigger="schedule",
        started_at=datetime(2026, 10, 19, 6, 0),
        finished_at=datetime(2026, 10, 19, 6, 2),
        stats_json={
            "new": 7,
            "filtered": 2,
            "scored": 4,
            "scoring_failed": 1,
            "sources": {"platsbanken": {"ok": True, "count": 9}, "linkedin": {"ok": False, "count": 0}},
        },
        errors_json={"linkedin": "Gmail-token <ogiltig>"},
    )

    html = client.get("/runs").text

    assert "2026-10-19 08:00" in html
    assert "Schema" in html
    assert "platsbanken" in html and "linkedin" in html
    assert "Gmail-token &lt;ogiltig&gt;" in html
    assert "7" in html


def test_runs_page_shows_only_latest_50(client, add_run):
    for i in range(55):
        add_run(trigger="manual", errors_json={f"källa{i:02d}": "fel"})

    html = client.get("/runs").text

    assert "källa54" in html
    assert "källa05" in html
    assert "källa04" not in html


def test_runs_page_without_runs(client):
    assert "Inga körningar ännu" in client.get("/runs").text


def test_runs_page_shows_spend_api(client, config, engine):
    config.ai.backend = "api"
    config.ai_monthly_budget_usd = 10.0
    with session_scope(engine) as session:
        session.add(AICall(model="claude-haiku-5-5", purpose="score", cost_usd=2.5))
        session.add(AICall(model="claude-sonnet-5-5", purpose="letter", cost_usd=1.0))

    html = client.get("/runs").text

    assert "AI-kostnad denna månad" in html
    assert "3.50 USD" in html
    assert "10.00 USD" in html
    assert "Körs via din Claude-prenumeration" not in html


def test_runs_page_shows_subscription_usage(client, config, engine):
    config.ai.backend = "claude_code"
    with session_scope(engine) as session:
        session.add(AICall(model="m", purpose="score", input_tokens=1200, output_tokens=300))
        session.add(AICall(model="m", purpose="letter", input_tokens=800, output_tokens=200))
        # förra månaden räknas inte
        session.add(
            AICall(
                model="m",
                purpose="score",
                input_tokens=99999,
                output_tokens=99999,
                at=datetime(2020, 1, 1),
            )
        )

    html = client.get("/runs").text

    assert "Körs via din Claude-prenumeration" in html
    assert "AI-anrop denna månad: 2" in html
    assert "Tokens in: 2000" in html
    assert "Tokens ut: 500" in html
    assert "USD" not in html


# --- inställningar -------------------------------------------------------------------------


def settings_form(**overrides) -> dict[str, str]:
    data = {
        "profile_name": "Jane Doe",
        "profile_email": "jane@example.se",
        "profile_phone": "070-1234567",
        "profile_linkedin_url": "https://www.linkedin.com/in/jane",
        "roles": "Backendutvecklare\nSystemutvecklare",
        "keywords": "python\n\n  fastapi  ",
        "exclude_keywords": "php",
        "exclude_companies": "Dåligt AB",
        "locations": "Göteborg\nStockholm",
        "seniority": "senior",
        "remote_ok": "on",
        "platsbanken": "on",
        "careers": "greenhouse,Exempel,exempel\n\n  lever , Annat AB , annat  \n",
        "threshold": "70",
        "schedule": "07:30\n16:45",
        "ai_backend": "api",
        "claude_bin": "claude",
        "ai_monthly_budget_usd": "12.5",
    }
    data.update(overrides)
    return data


def parse_form(html: str) -> dict[str, str]:
    """Läser ut formulärfältens värden ur sidan, som webbläsaren skulle skicka dem."""
    import html as html_lib
    import re

    form: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        if name is None:
            continue
        if 'type="checkbox"' in tag:
            if " checked" in tag:
                form[name.group(1)] = "on"
            continue
        value = re.search(r'value="([^"]*)"', tag)
        form[name.group(1)] = html_lib.unescape(value.group(1)) if value else ""
    for name, body in re.findall(r'<textarea name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        form[name] = html_lib.unescape(body.removeprefix("\n"))
    for name, block in re.findall(r'<select name="([^"]+)">(.*?)</select>', html, re.S):
        selected = re.search(r'<option value="([^"]*)" selected', block)
        form[name] = selected.group(1) if selected else ""
    return form


@pytest.fixture
def saved(state):
    calls: list[Config] = []
    state.on_config_saved = calls.append
    return calls


def test_settings_page_shows_current_values(client, config):
    config.preferences.roles = ["Backendutvecklare", "Data <engineer>"]
    config.sources.careers = []

    html = client.get("/settings").text

    assert 'value="Test Testsson"' in html
    assert 'value="test@example.se"' in html
    assert "Backendutvecklare\nData &lt;engineer&gt;</textarea>" in html
    assert "08:00\n12:00\n17:00</textarea>" in html


def test_settings_page_has_no_gmail_fields(client):
    html = client.get("/settings").text

    for removed in ("Torrkörning", "LinkedIn-aviseringar", "max_emails_per_day", "dry_run"):
        assert removed not in html


def test_setup_does_not_require_gmail(paths, config, make_pdf):
    make_ready(paths, make_pdf)
    config.ai.claude_bin = "sh"  # finns alltid på PATH

    assert not paths.data_dir.joinpath("gmail_token.json").exists()
    assert compute_setup_missing(paths, lambda: config, lambda: None) == []


def test_settings_roundtrip_writes_yaml(client, state, saved):
    response = client.post("/settings", data=settings_form(), follow_redirects=False)

    assert response.status_code == 303
    assert "Sparat" in client.get(response.headers["location"]).text
    written = load_config(state.paths.config)
    assert written.profile.name == "Jane Doe"
    assert written.profile.phone == "070-1234567"
    assert written.preferences.roles == ["Backendutvecklare", "Systemutvecklare"]
    assert written.preferences.exclude_companies == ["Dåligt AB"]
    assert written.preferences.remote_ok is True
    assert written.sources.platsbanken.enabled is True
    assert [(c.type, c.company, c.id) for c in written.sources.careers] == [
        ("greenhouse", "Exempel", "exempel"),
        ("lever", "Annat AB", "annat"),
    ]
    assert written.scoring.threshold == 70
    assert written.schedule == ["07:30", "16:45"]
    assert written.ai.backend == "api"
    assert written.ai_monthly_budget_usd == 12.5
    assert len(saved) == 1 and saved[0] == written
    assert [p.name for p in state.paths.config.parent.glob(".config.yaml*")] == []


def test_settings_page_shows_saved_careers_one_per_line(client, state, config):
    config.sources.careers = []
    client.post("/settings", data=settings_form())
    state.config = lambda: load_config(state.paths.config)

    html = client.get("/settings").text

    assert "greenhouse,Exempel,exempel\nlever,Annat AB,annat" in html


def test_settings_roundtrip_with_commas_in_values_is_lossless(client, state, config, saved):
    config.preferences.roles = ["Utvecklare, backend", "Data engineer"]
    config.preferences.exclude_companies = ["Acme, Inc.", "Foo AB"]
    config.preferences.locations = ["Stockholm, Sverige", "Göteborg"]
    config.sources.careers = [
        CareerSite(type="greenhouse", company="Foo, Inc", id="foo"),
        CareerSite(type="lever", company="Åkeri AB", id="akeri"),
    ]
    original = config.model_copy(deep=True)

    html = client.get("/settings").text
    form = parse_form(html)
    response = client.post("/settings", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert load_config(state.paths.config) == original
    assert saved == [original]


def test_careers_company_with_commas_is_parsed(client, state):
    client.post("/settings", data=settings_form(careers="greenhouse,Foo, Inc & Co,foo-id"))

    site = load_config(state.paths.config).sources.careers[0]
    assert (site.type, site.company, site.id) == ("greenhouse", "Foo, Inc & Co", "foo-id")


def test_settings_validation_error_shown(client, state, saved):
    response = client.post("/settings", data=settings_form(schedule="08:00\n25:00"))

    assert response.status_code == 400
    assert "ogiltig tid" in response.text
    assert "25:00" in response.text
    assert "08:00\n25:00</textarea>" in response.text  # inmatningen behålls
    assert 'value="Jane Doe"' in response.text
    assert not state.paths.config.exists()
    assert saved == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"careers": "greenhouse,Exempel,exempel\nbara-två,delar"}, "Rad 2"),
        ({"careers": "workday,Exempel,x"}, "Rad 1"),
        ({"careers": "lever,,x"}, "Rad 1"),
        ({"profile_name": "  "}, "Obligatoriskt"),
        ({"profile_email": "ingen-snabela"}, "Ogiltig e-postadress"),
        ({"threshold": "många"}, "heltal"),
        ({"threshold": "101"}, "mellan 0 och 100"),
        ({"ai_monthly_budget_usd": "gratis"}, "tal"),
        ({"ai_monthly_budget_usd": "12,5"}, "använd punkt som decimaltecken"),
        ({"ai_monthly_budget_usd": "nan"}, "ändligt tal"),
        ({"ai_monthly_budget_usd": "inf"}, "ändligt tal"),
        ({"ai_monthly_budget_usd": "-inf"}, "ändligt tal"),
        ({"threshold": "nan"}, "heltal"),
        ({"threshold": "inf"}, "heltal"),
        ({"ai_backend": "lokal"}, "Ogiltigt val"),
    ],
)
def test_settings_field_errors(client, saved, overrides, expected):
    response = client.post("/settings", data=settings_form(**overrides))

    assert response.status_code == 400
    assert expected in response.text
    assert saved == []


def test_settings_escapes_submitted_values(client):
    response = client.post(
        "/settings",
        data=settings_form(profile_name='"><script>alert(1)</script>', schedule="nej"),
    )

    assert response.status_code == 400
    assert "<script>alert(1)" not in response.text
    assert "&lt;script&gt;" in response.text


def test_settings_preserves_fields_not_in_form(client, state, config, saved):
    """Fält som formuläret inte skickar behålls från nuvarande inställningar."""
    config.profile.linkedin_url = "https://www.linkedin.com/in/behall"
    data = settings_form()
    del data["profile_linkedin_url"]

    client.post("/settings", data=data)

    assert load_config(state.paths.config).profile.linkedin_url == "https://www.linkedin.com/in/behall"


def test_settings_works_without_config_file(state, client):
    def missing() -> Config:
        raise FileNotFoundError(state.paths.config)

    state.config = missing

    response = client.get("/settings")

    assert response.status_code == 200
    assert 'name="profile_name"' in response.text
    assert "08:00\n12:00\n17:00</textarea>" in response.text

    saved_response = client.post("/settings", data=settings_form())
    assert saved_response.status_code == 200  # efter omdirigeringen
    assert state.paths.config.exists()


def test_invalid_config_file_shows_notice_and_is_backed_up_on_save(client, state, saved):
    broken = "profile: [oavslutad\n  nej: :"
    state.paths.config.write_text(broken, encoding="utf-8")

    html = client.get("/settings").text

    assert "config.yaml kunde inte läsas" in html

    response = client.post("/settings", data=settings_form(), follow_redirects=False)

    assert response.status_code == 303
    backup = state.paths.config.with_name("config.yaml.bak")
    assert backup.read_text(encoding="utf-8") == broken
    assert load_config(state.paths.config).profile.name == "Jane Doe"
    assert "config.yaml kunde inte läsas" not in client.get("/settings").text

    # En giltig fil säkerhetskopieras inte på nytt över den gamla kopian.
    client.post("/settings", data=settings_form(profile_name="Annan"))
    assert backup.read_text(encoding="utf-8") == broken


def test_schema_invalid_config_file_names_the_problem(client, state):
    state.paths.config.write_text(
        'profile: {name: "A", email: "a@x.se"}\nschedule: ["25:00"]\n', encoding="utf-8"
    )

    html = client.get("/settings").text

    assert "config.yaml kunde inte läsas" in html
    assert "schedule" in html and "ogiltig tid" in html


def test_missing_config_file_shows_no_notice(client, state):
    assert not state.paths.config.exists()

    assert "kunde inte läsas" not in client.get("/settings").text
    assert not state.paths.config.with_name("config.yaml.bak").exists()


# --- installationssida och omdirigering ------------------------------------------------


@pytest.fixture
def missing_setup(state):
    state.setup_missing = lambda: ["CV saknas", "AI saknas"]


def test_setup_redirect_when_missing(client, missing_setup):
    for method, path in [("get", "/"), ("get", "/jobs/1"), ("get", "/runs"), ("post", "/run")]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup"

    assert client.get("/setup").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_setup_redirect_does_not_apply_when_nothing_missing(client):
    assert client.get("/", follow_redirects=False).status_code == 200


def test_settings_post_is_allowed_while_setup_missing(client, missing_setup, saved):
    response = client.post("/settings", data=settings_form(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings")
    assert len(saved) == 1


def test_host_check_runs_before_setup_redirect(client, missing_setup):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    cross = client.post("/run", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert cross.status_code == 403


def test_setup_lists_missing_items(client, missing_setup):
    html = client.get("/setup").text

    assert "CV saknas" in html
    assert "AI saknas" in html
    assert 'href="/settings"' in html


def test_setup_all_done(client):
    html = client.get("/setup").text

    assert "Allt är klart" in html


def test_setup_items_are_escaped(client, state):
    state.setup_missing = lambda: ["<script>x</script>"]

    assert "<script>x</script>" not in client.get("/setup").text


def make_ready(paths, make_pdf):
    import shutil

    shutil.copy(make_pdf("CV"), paths.cv)
    paths.config.write_text("x", encoding="utf-8")


def test_compute_setup_missing_lists_everything_on_empty_install(paths, monkeypatch):
    monkeypatch.setattr("jobaplier.web.setup.claude_code_available", lambda claude_bin: False)

    missing = compute_setup_missing(paths, lambda: None, lambda: None)

    assert len(missing) == 3
    joined = "\n".join(missing)
    assert "data/cv.pdf" in joined
    assert "data/config.yaml" in joined
    assert "Claude Code hittas inte – installera och logga in med `claude`" in joined
    assert "gmail" not in joined.lower()
    assert "ANTHROPIC_API_KEY" not in joined


def test_compute_setup_missing_all_present(paths, config, make_pdf):
    make_ready(paths, make_pdf)
    config.ai.claude_bin = "sh"  # finns alltid på PATH

    assert compute_setup_missing(paths, lambda: config, lambda: None) == []


def test_setup_api_key_only_required_for_api_backend(paths, config, make_pdf):
    make_ready(paths, make_pdf)
    config.ai.claude_bin = "sh"

    config.ai.backend = "claude_code"
    assert compute_setup_missing(paths, lambda: config, lambda: None) == []

    config.ai.backend = "api"
    missing = compute_setup_missing(paths, lambda: config, lambda: None)
    assert len(missing) == 1 and "ANTHROPIC_API_KEY" in missing[0]
    assert compute_setup_missing(paths, lambda: config, lambda: "sk-test") == []


def test_claude_missing_only_reported_for_claude_code_backend(paths, config, make_pdf):
    make_ready(paths, make_pdf)
    config.ai.claude_bin = "finns-inte-på-riktigt-xyz"

    config.ai.backend = "claude_code"
    missing = compute_setup_missing(paths, lambda: config, lambda: None)
    assert len(missing) == 1 and "Claude Code hittas inte" in missing[0]

    config.ai.backend = "api"
    assert compute_setup_missing(paths, lambda: config, lambda: "sk-test") == []


def test_compute_setup_missing_treats_loader_error_as_missing_config(paths, make_pdf):
    make_ready(paths, make_pdf)

    def broken() -> Config:
        raise ValueError("trasig yaml")

    missing = compute_setup_missing(paths, broken, lambda: None)

    assert any("data/config.yaml" in item for item in missing)



# --- strukturerat CV ------------------------------------------------------------------------


def test_cv_page_shows_document(client, cv_document):
    cv_document.document = CVDocument(
        name="Jane <Doe>",
        email="jane@example.se",
        phone="070-111 22 33",
        summary="Erfaren backendutvecklare.",
        experience=[
            Experience(
                id="e1",
                title="Backendutvecklare",
                employer="Acme & Co",
                location="Göteborg",
                period="2018 – 2024",
                bullets=["Byggde API:er i <Python>"],
            )
        ],
        education=[
            Education(
                id="u1", degree="Civilingenjör", school="Chalmers", period="2012 – 2017",
                details=["Datateknik"],
            )
        ],
        skills=["Python", "SQL"],
        languages=["Svenska", "Engelska"],
        other=[OtherSection(heading="Certifikat", items=["AWS"])],
    )

    response = client.get("/cv")

    assert response.status_code == 200
    html = response.text
    assert "Jane &lt;Doe&gt;" in html
    assert "jane@example.se" in html and "070-111 22 33" in html
    assert "Erfaren backendutvecklare." in html
    assert "Backendutvecklare" in html and "Acme &amp; Co" in html
    assert "Göteborg" in html and "2018 – 2024" in html
    assert "Byggde API:er i &lt;Python&gt;" in html
    assert "Civilingenjör" in html and "Chalmers" in html and "Datateknik" in html
    assert "Python" in html and "SQL" in html
    assert "Svenska" in html and "Engelska" in html
    assert "Certifikat" in html and "AWS" in html
    for heading in ("Profil", "Erfarenhet", "Utbildning", "Kompetenser", "Språk"):
        assert f">{heading}</h2>" in html
    assert "Ditt CV har inte lästs ut än" not in html


def test_cv_page_shows_fidelity_warnings(client, cv_document, cv_warnings):
    cv_document.document = CVDocument(name="Jane Doe")
    cv_warnings.append('Kontrollera i ditt CV: "<Acme>" hittades inte ordagrant i cv.pdf')

    html = client.get("/cv").text

    assert "Kontrollera i ditt CV: &#34;&lt;Acme&gt;&#34; hittades inte ordagrant" in html


def test_cv_page_without_fidelity_warnings_has_no_banner(client, cv_document):
    cv_document.document = CVDocument(name="Jane Doe")

    assert "Kontrollera i ditt CV" not in client.get("/cv").text


def test_cv_page_without_document(client, cv_document):
    response = client.get("/cv")

    assert response.status_code == 200
    assert (
        "Ditt CV har inte lästs ut än – det görs när du klickar Ansök på ett jobb "
        "(eller Försök igen på ett utkast)" in response.text
    )
    assert cv_document.calls == 1


def test_nav_links_to_cv(client):
    assert '<a href="/cv">CV</a>' in client.get("/").text
