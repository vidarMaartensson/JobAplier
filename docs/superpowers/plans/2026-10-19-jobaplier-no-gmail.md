# JobAplier utan Gmail – implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ta bort Gmail-beroendet. Inför ett ansökningspaket med kopierbar text och två nedladdningsbara PDF:er, ett skräddarsytt CV och ett personligt brev. Ersätt LinkedIn-aviseringarna med import av inklistrade jobblänkar.

**Architecture:**
- Det strukturerade CV:t (`CVDocument`) byggs en gång per CV-fil och cachas i en ny tabell.
- Per utkastversion sparar Claude sina val (`TailoredCV`) i en ny tabell. Koden bygger det renderade CV:t (`RenderedCV`) via `verify_tailored`, så att titlar, arbetsgivare, perioder och utbildningar alltid kommer från originalet.
- Paketsidan, nedladdningarna och formulärifyllningen använder samma funktion för CV-PDF (`build_cv_pdf`). Den faller tillbaka på `data/cv.pdf` om ett skräddarsytt CV saknas.

**Tech Stack:** samma som tidigare: Python 3.12, FastAPI/Jinja2/HTMX, SQLModel/SQLite, WeasyPrint, Playwright, Claude Code CLI / anthropic SDK, pytest. Google-biblioteken tas bort.

**Spec:** `docs/superpowers/specs/2026-10-19-jobaplier-no-gmail-design.md`. Den ändrar `docs/superpowers/specs/2026-10-19-jobaplier-design.md`, som fortfarande gäller i allt det tillägget inte nämner.

## Global Constraints

- All text som användaren ser är på svenska. AI-prompter får vara på engelska.
- Modeller: `WRITING_MODEL` (`claude-sonnet-5-5`) för `purpose="cv_document"` och `purpose="tailor_cv"`, med `effort="medium"`.
- Titlar, arbetsgivare, orter och perioder för anställningar, samt alla utbildningsfält, i det renderade CV:t kopieras alltid från `CVDocument` av koden, aldrig från Claudes svar.
- Det skräddarsydda CV:t skrivs på annonsens språk (`sv`/`en`). Rubrikerna är exakt Profil/Erfarenhet/Utbildning/Kompetenser/Språk respektive Profile/Experience/Education/Skills/Languages.
- Nya tabeller skapas bara av `init_db` (`create_all`). Befintliga tabeller får **inte** nya kolumner, så ingen migrering behövs.
- Filnamn: `CV_<Namn>_<Företag>.pdf` och `Personligt_brev_<Företag>.pdf`. Namn och företag translittereras (å→a, ä→a, ö→o, é→e, ü→u). Övriga tecken som inte är `[A-Za-z0-9]` blir `_`, flera `_` slås ihop, och `_` i början och slutet tas bort. Varje del är högst 60 tecken.
- LinkedIn-import: högst 20 jobb-id per import. Ett försök per sida, och 1,0 s paus mellan sidorna.
- Annonstext och CV-text är osäker data. Den escapas i HTML och PDF, och prompterna säger att den är data, inte instruktioner.
- Inga nätverksanrop och inga riktiga AI-anrop i testsviten.
- Varje uppgift avslutas med att hela sviten passerar: `uv run pytest -q -W error`.

## Review Focus

1. **Gammal `config.yaml` med borttagna nycklar** (`sources.linkedin_alerts`, `apply.dry_run`, `apply.max_emails_per_day`) laddas utan fel. Nästa sparning från inställningssidan skriver inte ut dem. Test i Task 2.
2. **Utkastet i en gammal flik:** *Jag har skickat* för en äldre utkastversion ger 409 och markerar ingenting. Test i Task 6 och Task 8.
3. **CV:t byttes ut efter att utkastet skapades** (id:n i det sparade `TailoredCV` finns inte i det nya `CVDocument`). Nedladdningen ger då ändå ett giltigt CV med alla nya anställningar och originalets punkter, plus varningar, och inte ett fel. Test i Task 4 och Task 6.
4. **Svenska tecken och specialtecken i namn och företag** ("Vidar Mårtensson", "Åhléns & Co AB") ger ASCII-säkra filnamn och en giltig `Content-Disposition`. Test i Task 5 och Task 8.
5. **LinkedIn-länkar i olika format** (`/comm/`, slug-id som `.../jobs/view/backend-dev-at-x-4012345678/`, spårningsparametrar, dubbletter, blandat med skräprader, fler än 20) tolkas rätt. Test i Task 9.

---

## Filstruktur

```
src/jobaplier/
  sources/linkedin_page.py      NY  (flyttat ur linkedin_alerts.py) fetch_job_page, parse_job_page
  sources/linkedin_links.py     NY  parse_linkedin_links, LinkedInLinksSource
  sources/linkedin_alerts.py    TAS BORT
  gmail.py                      TAS BORT
  apply/email.py                TAS BORT
  apply/package.py              NY  ämne/brödtext/adresskontroll (flyttat ur email.py), mailto, filnamn, build_cv_pdf
  profile/cv_document.py        NY  CVDocument, load_or_build_cv_document
  apply/tailor.py               NY  TailoredCV, tailor_cv, RenderedCV, verify_tailored, with_contact_fallback
  apply/cv_pdf.py               NY  render_cv_pdf
  apply/templates/cv.html       NY
  models.py                     + CVDocumentRecord (cv_documents), TailoredCVRecord (tailored_cvs)
  status.py                     + draft_ready → submitted
  apply/service.py              CV i start_draft/regenerate/save_edit, retry_tailor, mark_sent_manually; mejlkanalen bort
  apply/form/applier.py         CV-fil via injicerad leverantör
  pipeline/run.py               skip_filter-attribut på källor
  web/routes_drafts.py          paketsida, nedladdningar, skickat, försök igen
  web/routes_admin.py           /cv; inställningsfält bort
  web/routes_jobs.py            LinkedIn-import; Ansök för alla sätt
  web/static/copy.js            NY
  main.py, cli.py, web/setup.py, config.py, README.md, pyproject.toml   Gmail bort, ny ihopkoppling
```

---

### Task 1: Flytta det som ska behållas ut ur Gmail-modulerna

Ingen beteendeändring. Det förbereder Task 2, så att borttagningen inte tar något som behövs med sig.

**Files:**
- Create: `src/jobaplier/sources/linkedin_page.py`, `src/jobaplier/apply/package.py`
- Modify: `src/jobaplier/sources/linkedin_alerts.py`, `src/jobaplier/apply/email.py` (importerar nu från de nya modulerna)
- Test: `tests/sources/test_linkedin_page.py`, `tests/apply/test_package.py`. Flytta de tester i `tests/sources/test_linkedin_alerts.py` och `tests/apply/test_email.py` som gäller sidtolkning, ämne, brödtext och adresskontroll.

**Interfaces:**
- Produces:
  - `linkedin_page.JOB_URL = "https://www.linkedin.com/jobs/view/{id}"`
  - `@dataclass JobPage`: `title: str`, `company: str`, `location: str | None`, `description: str`
  - `parse_job_page(html: str) -> JobPage | None` (`None` om titel saknas)
  - `fetch_job_page(http: httpx.Client, job_id: str) -> JobPage | None`. Gör ett försök (`get_with_retry(..., attempts=1)`). `SourceError`, status som inte är 200 och en sida som inte går att tolka ger `None`.
  - `package.BODY_SV`, `package.BODY_EN` (exakt dagens texter), `application_subject(job: Job) -> str`, `email_body(job: Job, draft: Draft, config: Config) -> str` (väljer `BODY_EN` om `draft.language == "en"`, annars `BODY_SV`), `valid_single_address(address: str) -> bool`

- [ ] **Step 1:** Flytta de befintliga testerna för sidtolkning, ämne, brödtext och adresskontroll till de nya testfilerna och peka om importerna. Kör dem. Förväntat: FAIL (import).
- [ ] **Step 2:** Flytta koden. `linkedin_alerts.py` och `email.py` importerar sedan från de nya modulerna, så att de fortsätter fungera fram till Task 2.
- [ ] **Step 3:** `uv run pytest -q -W error`. Förväntat: PASS, med samma antal tester som innan flytten.
- [ ] **Step 4:** Commit `refactor: extract LinkedIn page parsing and email package helpers`

---

### Task 2: Ta bort Gmail och mejlutskick

**Files:**
- Delete: `src/jobaplier/gmail.py`, `src/jobaplier/apply/email.py`, `src/jobaplier/sources/linkedin_alerts.py`, `tests/test_gmail.py`, `tests/apply/test_email.py`, `tests/sources/test_linkedin_alerts.py`, `tests/fixtures/gmail/`, fixturen `tests/fixtures/linkedin/alert.html` (behåll `job_page.html`). Ta också bort `FakeGmail` ur `tests/fakes.py`.
- Modify:
  - `config.py`: ta bort `SourcesConfig.linkedin_alerts`, `ApplyConfig.dry_run` och `max_emails_per_day`. `ApplyConfig` tas bort om den blir tom, och `Config.apply` försvinner då. Ta bort `Paths.gmail_credentials` och `gmail_token`.
  - `config.example.yaml`
  - `apply/base.py`: `AmbiguousSendError` och `DailyLimitReached` tas bort.
  - `apply/service.py`: mejlkanalen i `_CHANNELS`, `emails_sent_today` och `CHECK_GMAIL` tas bort.
  - `main.py`: `LazyGmail`, `LINKEDIN_NO_GMAIL`, LinkedIn-delen av `build_sources`, `EmailApplier` och `sweep_interrupted_emails` tas bort. Svepet för utkast och körningar behålls.
  - `cli.py`: `gmail-login` tas bort.
  - `web/setup.py`: `GMAIL_MISSING` tas bort.
  - `web/routes_admin.py` och `templates/settings.html`: fälten för LinkedIn-aviseringar, torrkörning och dagstak tas bort.
  - `templates/draft.html` och `submitted.html`: torrkörningsbanner och mejlutfall tas bort. Mejljobb får tills vidare samma vy som jobb med okänt ansökningssätt, och Task 8 bygger paketsidan.
  - `pyproject.toml` (sedan `uv lock`), `README.md` och de tester som rör det borttagna.

**Interfaces:**
- Produces:
  - `Config` utan de borttagna fälten. Pydantic ignorerar okända nycklar, och det beteendet ska behållas: lägg inte till `extra="forbid"`.
  - `ApplyService(engine, ai, paths, config, appliers)` där `appliers` bara innehåller `ApplyMethod.form`. `approve_and_submit` på ett jobb som inte är formulärjobb kastar `NoAutoApply`.

- [ ] **Step 1: Skriv failing tests:**
  - `tests/test_config.py::test_old_config_with_removed_keys_loads` (Review Focus 1): YAML med `sources.linkedin_alerts: {enabled: true}`, `apply: {dry_run: false, max_emails_per_day: 3}` laddas, och `save_config` och sedan en läsning av filen som text visar att `linkedin_alerts`, `dry_run` och `max_emails_per_day` saknas.
  - `tests/web/test_admin.py::test_setup_does_not_require_gmail`: cv, config och AI finns men ingen token, så `setup_missing() == []`.
  - `tests/web/test_admin.py::test_settings_page_has_no_gmail_fields`: HTML:en innehåller inte "Torrkörning", "LinkedIn-aviseringar" eller "max_emails_per_day".
  - `tests/test_cli.py::test_no_gmail_login_command`: `gmail-login` ger ett argparse-fel.
  - `tests/test_no_gmail.py::test_no_gmail_imports`: inga filer under `src/` innehåller `googleapiclient`, `google.oauth2` eller `jobaplier.gmail`.
  - `tests/apply/test_service.py::test_approve_email_job_raises_no_auto_apply`.
- [ ] **Step 2:** Kör de nya testerna. Förväntat: FAIL.
- [ ] **Step 3:** Ta bort och ändra filerna enligt listan. Kör `uv remove google-api-python-client google-auth google-auth-oauthlib`.
- [ ] **Step 4:** `uv run pytest -q -W error`. Förväntat: PASS. Kör också `grep -rn "gmail" src/ -i`, som ska ge träffar bara i README-historik eller inga alls.
- [ ] **Step 5:** Commit `feat: remove Gmail login and email sending`

---

### Task 3: Strukturerat CV

**Files:**
- Create: `src/jobaplier/profile/cv_document.py`
- Modify: `src/jobaplier/models.py` (lägg till `CVDocumentRecord`)
- Test: `tests/test_cv_document.py`

**Interfaces:**
- Consumes: `extract_cv_text`, `CVMissing`, `CVUnreadable` (`profile/cv.py`); `AIClient`, `WRITING_MODEL`
- Produces:
  - Pydantic-modeller enligt spec 3.1: `Experience(id, title, employer, location, period, bullets)`, `Education(id, degree, school, period, details)`, `OtherSection(heading, items)`, `CVDocument(language, name, email, phone, location, linkedin_url, summary, experience, education, skills, languages, other)`. Alla strängfält har standardvärdet `""` och alla listor `[]`, utom `language`, som har `"sv"`.
  - `CVDocumentRecord` (tabell `cv_documents`): `id`, `cv_hash: str (index)`, `document_json: dict (MutableDict JSON)`, `created_at`
  - `renumber(doc: CVDocument) -> CVDocument` (anställningar `e1..eN` och utbildningar `u1..uN` i listordning)
  - `load_or_build_cv_document(session: Session, cv_path: Path, ai: AIClient) -> CVDocument`. Samma cache-mönster som `load_or_build_profile`: sha256 av filen och senaste raden. Vid ny hash anropas `ai.complete(model=WRITING_MODEL, purpose="cv_document", schema=CVDocument, effort="medium")`, svaret numreras om, raden sparas och flushas.
  - Systemprompt: kopiera ordagrant från CV:t, hitta inte på något, behåll CV:ts ordning, `period` exakt som den står, och ange CV:ts språk.

- [ ] **Step 1: Skriv failing tests:**
  - `test_builds_and_renumbers`: FakeAI returnerar anställningar med id:n `"x"` och `"y"`, och resultatet har `e1` och `e2`. Prompten innehåller CV-texten, och `purpose == "cv_document"`.
  - `test_reuses_when_hash_unchanged`: ett AI-anrop totalt.
  - `test_rebuilds_when_cv_changes`
  - `test_missing_cv_raises`: `CVMissing`, inga AI-anrop.
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: structured CV document cached per CV file`

---

### Task 4: Anpassning och kodkontroll

**Files:**
- Create: `src/jobaplier/apply/tailor.py`
- Test: `tests/apply/test_tailor.py`

**Interfaces:**
- Consumes: `CVDocument` (Task 3); `Job`; `ProfileInfo` (`config.py`); `AIClient`, `WRITING_MODEL`, `AIResult`
- Produces:
  - `TailoredExperience(id: str, bullets: list[str])`, `TailoredCV(language: Literal["sv","en"], headline: str, summary: str, experience: list[TailoredExperience], skills: list[str], education_order: list[str])`
  - `tailor_cv(job: Job, doc: CVDocument, ai: AIClient, *, instruction: str | None = None) -> AIResult[TailoredCV]`. Prompten innehåller `doc.model_dump_json()`, annonsens titel, företag och beskrivning, reglerna i spec 3.2 och instruktionen om den finns.
  - `RenderedExperience(title, employer, location, period, bullets)`, `RenderedEducation(degree, school, period, details)`, `RenderedCV(language, name, email, phone, location, linkedin_url, headline, summary, experience, education, skills, languages, other)`
  - `verify_tailored(t: TailoredCV, doc: CVDocument) -> tuple[RenderedCV, list[str]]` med reglerna och de exakta varningstexterna i spec 3.3
  - `with_contact_fallback(cv: RenderedCV, profile: ProfileInfo) -> RenderedCV` (tomma `name`/`email`/`phone`/`linkedin_url` hämtas från `profile`)
  - `original_cv(doc: CVDocument) -> RenderedCV` (oanpassat: dokumentets ordning, profiltext och punkter, och `language=doc.language`; används när utkastets `TailoredCV` saknas)

Algoritm för talkontrollen (spec 3.3): `re.findall(r"\d+(?:[.,]\d+)?", bullet)`. Varje tal måste finnas som delsträng i originalpunkterna för samma anställning, sammanfogade med mellanslag. Annars ges en varning.

- [ ] **Step 1: Skriv failing tests:**
  - `test_titles_and_periods_always_from_original`: originalet har `e1` med titeln "Utvecklare" och perioden "2021 – nu". `TailoredCV` refererar `e1`, och resultatet har exakt originalets titel, arbetsgivare och period.
  - `test_unknown_id_dropped_with_warning`: `"e9"` ger varningen "Okänd post ignorerades: e9".
  - `test_omitted_entries_appended_with_original_bullets`: originalet har e1–e3 och svaret bara e2. Ordningen blir e2, e1, e3, och e1 och e3 har originalpunkter. Samma för utbildning.
  - `test_empty_bullets_get_originals`
  - `test_unknown_skill_removed_with_warning`: "Kubernetes" finns inte i originalet och ger varningen "Kompetens som inte finns i ditt CV togs bort: Kubernetes". Kompetenser som finns, oavsett skiftläge och mellanslag, behålls i Claudes ordning.
  - `test_number_not_in_original_warns`: punkten "Ökade försäljningen 40 %" mot originalet "ökade försäljningen 25 %" ger varningen "Kontrollera siffran 40 i: Ökade försäljningen 40 %", och punkten behålls.
  - `test_cv_replaced_after_draft` (Review Focus 3): `TailoredCV` med id:n e1–e2 mot ett nytt dokument med andra titlar. Resultatet innehåller de nya anställningarna med originalpunkter och kastar inget fel.
  - `test_contact_fallback`
  - `test_original_cv`
  - `test_tailor_prompt`: modell, `purpose="tailor_cv"`, dokumentets JSON och annonstexten finns i prompten, och instruktionen finns med när den ges.
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: tailor CV per job with code-enforced facts`

---

### Task 5: CV-PDF och filnamn

**Files:**
- Create: `src/jobaplier/apply/cv_pdf.py`, `src/jobaplier/apply/templates/cv.html`
- Modify: `src/jobaplier/apply/package.py` (filnamn)
- Test: `tests/apply/test_cv_pdf.py`, `tests/apply/test_package.py`

**Interfaces:**
- Consumes: `RenderedCV` (Task 4); `extract_cv_text` (för tester)
- Produces:
  - `HEADINGS: dict[str, dict[str, str]]` (exakt enligt Global Constraints)
  - `render_cv_pdf(cv: RenderedCV, out: Path) -> Path`: Jinja2-miljö med `autoescape=True` och mallen laddad via `PackageLoader`/`FileSystemLoader` från `apply/templates`. Skapar `out.parent` och returnerar `out`.
  - `package.safe_filename_part(text: str) -> str`, `package.cv_filename(name: str, company: str) -> str`, `package.letter_filename(company: str) -> str`. Tom del, efter rensning, ersätts med `"CV"` respektive `"Foretag"`.
- Mallen: A4, marginal 1,8 cm, sans-serif 10,5 pt. Avsnitt med tom lista visas inte. Punkter renderas som `<ul>`.

- [ ] **Step 1: Skriv failing tests:**
  - `test_renders_pdf_with_content`: `%PDF` och utläst text innehåller namn, rubrik, titel och en punkt.
  - `test_headings_follow_language` (sv och en)
  - `test_escapes_html`: `<script>x</script>` i en punkt finns som text och ingen tagg tolkas. Utläst text innehåller `<script>`.
  - `test_empty_sections_hidden`
  - `test_filenames` (Review Focus 4): `cv_filename("Vidar Mårtensson", "Åhléns & Co AB") == "CV_Vidar_Martensson_Ahlens_Co_AB.pdf"`, `letter_filename("Åhléns & Co AB") == "Personligt_brev_Ahlens_Co_AB.pdf"`, maxlängd 60 per del, och tom del.
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: render tailored CV as PDF`

---

### Task 6: ApplyService – CV i utkasten, försök igen och manuellt skickad

**Files:**
- Modify: `src/jobaplier/models.py` (`TailoredCVRecord`), `src/jobaplier/status.py`, `src/jobaplier/apply/service.py`, `src/jobaplier/apply/package.py` (`build_cv_pdf`)
- Test: `tests/apply/test_service.py`, `tests/test_status.py`, `tests/apply/test_package.py`

**Interfaces:**
- Consumes: Task 3–5
- Produces:
  - `TailoredCVRecord` (tabell `tailored_cvs`): `id`, `draft_id: int (FK drafts.id, unique)`, `tailored_json: dict (MutableDict JSON)`, `warnings_json: list (MutableList JSON)`, `created_at`
  - `ALLOWED_TRANSITIONS[draft_ready]` får även `submitted`.
  - `ApplyService.start_draft` och `regenerate`: efter brevet (som i dag) körs `self._tailor(job, draft_id, instruction)`. `_tailor` bygger och hämtar `CVDocument`, anropar `tailor_cv` och `verify_tailored` och sparar `TailoredCVRecord(tailored_json=t.model_dump(mode="json"), warnings_json=warnings)`. `AIError`, `CVMissing` och `CVUnreadable` från `_tailor` loggas och sväljs, så att utkastet ändå skapas. `regenerate` skickar instruktionen vidare.
  - `ApplyService.save_edit`: den nya versionen får en kopia av föregående versions `TailoredCVRecord`, om den finns.
  - `ApplyService.retry_tailor(job_id: int) -> Draft`: kräver `draft_ready`, kör `_tailor` för den senaste versionen och ersätter eller skapar raden. Här sväljs **inte** felet: `AIError` och CV-fel kastas vidare.
  - `ApplyService.tailored_for(draft_id: int) -> tuple[TailoredCV, list[str]] | None`
  - `ApplyService.mark_sent_manually(job_id: int, draft_id: int) -> Application` enligt spec 3.8. Kanalen är `Channel.email` om `apply_method == email`, annars `Channel.manual`. Tillåts för `email` och `unknown`, och ett formulärjobb ger `AlreadyHandled("Formulärjobb markeras som skickade efter ifyllning")`. En äldre version ger `AlreadyHandled("En nyare version av utkastet finns – ladda om sidan")`.
  - `package.build_cv_pdf(engine: Engine, paths: Paths, config: Config, ai: AIClient, draft_id: int, out: Path) -> tuple[Path, bool]`. Med `TailoredCVRecord` blir det `load_or_build_cv_document`, sedan `verify_tailored`, `with_contact_fallback` och `render_cv_pdf(out)`, med returen `(out, True)`. Utan rad returneras `(paths.cv, False)`. Saknas `paths.cv` kastas `CVMissing`. `ai` används bara om dokumentet måste byggas om, när CV:t har bytts.

- [ ] **Step 1: Skriv failing tests:**
  - `test_status.py`: kanten `draft_ready → submitted` är tillåten.
  - `test_start_draft_creates_letter_and_cv`: en `TailoredCVRecord` för version 1, och `purpose`-ordningen är letter, cv_document, tailor_cv (profilen finns redan i fixturen).
  - `test_start_draft_without_cv_when_tailoring_fails`: FakeAI kastar `AIError` för `TailoredCV`. Utkastet finns, jobbet är `draft_ready` och ingen rad skapas.
  - `test_regenerate_creates_both_with_instruction`: instruktionen finns i både brev- och CV-prompten.
  - `test_save_edit_copies_tailored_cv`
  - `test_retry_tailor_adds_row`, `test_retry_tailor_raises_on_ai_error`
  - `test_mark_sent_manually_email` (kanal `email`, jobbet `submitted`, `dry_run is False`) och `test_mark_sent_manually_unknown` (kanal `manual`)
  - `test_mark_sent_manually_stale_version` (Review Focus 2): version 1 när version 2 finns ger `AlreadyHandled` och jobbet förblir `draft_ready`.
  - `test_mark_sent_manually_form_job_refused`, `test_mark_sent_manually_wrong_status`
  - `test_package.py::test_build_cv_pdf_tailored` (en PDF i `out` och `True`), `test_build_cv_pdf_falls_back_to_original` (`(paths.cv, False)`), `test_build_cv_pdf_after_cv_replaced` (Review Focus 3: CV-fil och hash byts efter utkastet, resultatet är en giltig PDF med `True`, och dokumentet byggs om)
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera. Mönstret i `service.py` följs: lås och `session_scope` per steg, AI-anrop utan lås, och `_detach` på det som returneras.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: drafts carry a tailored CV; mark applications as sent manually`

---

### Task 7: Formulärifyllning laddar upp det skräddarsydda CV:t

**Files:**
- Modify: `src/jobaplier/apply/form/applier.py`, `src/jobaplier/apply/form/fields.py`
- Test: `tests/apply/form/test_applier.py`

**Interfaces:**
- Consumes: `build_cv_pdf` (Task 6), via en injicerad callable
- Produces:
  - `FormApplier(config, paths, adapters, headless=False, *, cv_pdf: Callable[[Job, Draft], Path] | None = None, allow_file_urls=False)`. Med `cv_pdf=None` används `paths.cv`, som i dag. Annars anropas `cv_pdf(job, draft)` innan uppdraget köas, och ett fel ger `SubmitError("Kunde inte skapa CV:t som PDF: …")`.
  - `Applicant.from_config(config, paths, letter_text, letter_pdf, *, cv_path: Path | None = None)` (standard `paths.cv`)

- [ ] **Step 1: Skriv failing tests:** `test_uses_provided_cv_pdf` (det uppladdade filnamnet i fixturformulärets filfält är leverantörens fil), `test_cv_provider_error_becomes_submit_error` och `test_default_uses_original_cv`.
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: form filling uploads the tailored CV`

---

### Task 8: Paketsidan, nedladdningarna och `/cv`

**Files:**
- Modify: `src/jobaplier/web/routes_drafts.py`, `src/jobaplier/web/routes_admin.py`, `src/jobaplier/web/routes_jobs.py` (Ansök för alla ansökningssätt), `src/jobaplier/web/state.py`, `src/jobaplier/web/templates/draft.html`, `_actions.html`, `base.html` (navigering till CV), `src/jobaplier/apply/package.py` (`mailto_link`)
- Create: `src/jobaplier/web/templates/cv.html`, `src/jobaplier/web/static/copy.js`
- Test: `tests/web/test_drafts.py`, `tests/web/test_admin.py`, `tests/web/test_jobs.py`, `tests/apply/test_package.py`

**Interfaces:**
- Consumes: `ApplyService.tailored_for/retry_tailor/mark_sent_manually` (Task 6), `build_cv_pdf`, `render_letter_pdf`, `cv_filename`, `letter_filename`, `application_subject`, `email_body`, `valid_single_address`, `load_or_build_cv_document`
- Produces:
  - `AppState` får `cv_pdf: Callable[[int, Path], tuple[Path, bool]]` (draft_id och målfil, ihopkopplat i Task 10) och `cv_document: Callable[[], CVDocument | None]` (`None` om CV:t saknas, inte kan läsas eller inte kan struktureras). Testernas fixture tillhandahåller fakes.
  - `package.mailto_link(to: str, subject: str, body: str) -> str | None`. Returnerar `None` om `valid_single_address(to)` är falskt. Annars `f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"`, där `urllib.parse.quote` används med `safe=""`.
  - Routes:
    - `GET /jobs/{id}/draft`: paketsidan enligt spec 3.7, med tabellen per `apply_method`.
    - `GET /jobs/{id}/draft/cv.pdf` och `GET /jobs/{id}/draft/letter.pdf`: `FileResponse` med `filename=` satt till de säkra namnen och `media_type="application/pdf"`. Renderingen görs i en `tempfile.TemporaryDirectory`, som städas med `BackgroundTask` efter svaret. `CVMissing` ger en svensk sida med status 404 och länk till `/setup`, och andra renderingsfel en svensk sida med status 500.
    - `POST /jobs/{id}/draft/sent` (formfält `draft_id`): `mark_sent_manually`, sedan 303 till `/jobs/{id}/draft?msg=sent`. `AlreadyHandled` ger 409 med texten.
    - `POST /jobs/{id}/draft/tailor`: `retry_tailor`. Vid fel visas paketsidan med felet.
    - `GET /cv`: strukturerat CV, eller "CV:t kunde inte läsas ut än – det görs när du skapar ditt första utkast".
  - `POST /jobs/{id}/apply` tillåts för alla `apply_method` (tidigare avvisades `unknown`). *Fyll i automatiskt* (`approve`) finns kvar bara för `form`.
  - `copy.js`: en klick-hanterare på `[data-copy-target]` som kopierar `document.getElementById(target).value || textContent` via `navigator.clipboard.writeText` och visar "Kopierat!" i 2 s. Den laddas från `/static/copy.js` med `defer`.

- [ ] **Step 1: Skriv failing tests:**
  - `test_package_email_job`: mottagare, ämne och brödtext med `data-copy-target`, en korrekt kodad `mailto:`-länk och knapparna för nedladdning och *Jag har skickat*.
  - `test_package_email_invalid_address_no_mailto`
  - `test_package_form_job`: *Fyll i automatiskt*, *Öppna formuläret* och nedladdningarna.
  - `test_package_unknown_job`: *Öppna annonsen* och *Jag har skickat*.
  - `test_package_shows_cv_warnings` och `test_package_shows_fallback_without_tailored_cv` (reservtexten och *Försök igen*)
  - `test_download_cv_tailored` och `test_download_cv_fallback_original`: `content-type: application/pdf`, och `content-disposition` innehåller `attachment` och `CV_Vidar_Martensson_Ahlens_Co_AB.pdf` (Review Focus 4).
  - `test_download_letter`, `test_download_cv_missing_cv_404`
  - `test_mark_sent_redirects`, `test_mark_sent_stale_409` (Review Focus 2)
  - `test_tailor_retry`
  - `test_cv_page_shows_document` och `test_cv_page_without_document`
  - `test_apply_allowed_for_unknown_method` (ersätter testet som förväntade avvisning)
  - `test_copy_js_served` och `test_package_mailto_encoding` (i `test_package.py`, med å, & och radbrytning)
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera. Mallmönstren i `draft.html` och `_actions.html` följs: autoescape och `safe_url` på annons-URL:er. `mailto:`-länken byggs bara av `mailto_link` och går därför inte genom `safe_url`.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: application package page with tailored CV and letter downloads`

---

### Task 9: Import av LinkedIn-länkar

**Files:**
- Create: `src/jobaplier/sources/linkedin_links.py`
- Modify: `src/jobaplier/pipeline/run.py`, `src/jobaplier/sources/base.py` (dokumentera `skip_filter`), `src/jobaplier/web/routes_jobs.py`, `src/jobaplier/web/state.py`, `src/jobaplier/web/templates/jobs.html`
- Test: `tests/sources/test_linkedin_links.py`, `tests/pipeline/test_run.py`, `tests/web/test_jobs.py`

**Interfaces:**
- Consumes: `fetch_job_page`, `JOB_URL` (Task 1); `RawJob`, `ApplyInfo`; `run_pipeline`
- Produces:
  - `MAX_LINKS = 20`
  - `parse_linkedin_links(text: str) -> tuple[list[str], list[str]]`: id:n är unika i första förekomstens ordning, och övriga icke-tomma rader avvisas. Regex: `linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#\s]*-)?(\d+)`.
  - `class TooManyLinks(ValueError)`: kastas av `parse_linkedin_links` när antalet id:n är större än `MAX_LINKS`. Meddelandet är "Högst 20 länkar åt gången".
  - `LinkedInLinksSource(job_ids: list[str], http: httpx.Client, sleep: Callable[[float], None] = time.sleep)`, med `name = "linkedin_links"` och `skip_filter = True`. `fetch(since)` ignorerar `since` och skapar ett `RawJob` per id via `fetch_job_page`: `source="linkedin_links"`, `external_id=id`, `url=JOB_URL.format(id=id)`, `published_at=None`, `apply=ApplyInfo(ApplyMethod.unknown)`. När `JobPage` är `None` blir det `incomplete=True`, titeln `f"LinkedIn-jobb {id}"`, företaget `"Okänt"` och beskrivningen `""`. Pausen är 1,0 s mellan sidorna och inte före den första.
  - `run.py::_collect`: om `getattr(source, "skip_filter", False)` är sant hoppas `hard_filter` över för källans nya jobb.
  - `AppState.import_links: Callable[[list[str]], None]`: startar en körning med bara `LinkedInLinksSource(ids)` i bakgrunden, och kastar `RunInProgress` som `run_now`. Kopplas ihop i Task 10.
  - Route `POST /import/linkedin` (formfält `links`):
    - Fler än 20 id:n ger sidan omrenderad med felet, och textrutan innehåller den inklistrade texten.
    - Inga giltiga id:n ger samma omrendering med meddelandet "Inga LinkedIn-jobblänkar hittades".
    - Vid `RunInProgress` renderas sidan om med "En sökning pågår redan – försök igen om en stund" och den inklistrade texten.
    - Annars blir det en 303 till `/?msg=imported&n=<antal>`. Flash-texten "N länkar lades till – poängsätts nu" byggs från vitlistan, och `n` tolkas som `int`.
    - Avvisade rader visas escapade under rutan.
  - Startsidan har rutan **Lägg till jobb från LinkedIn** (`<textarea name="links">`, knappen **Lägg till**).

- [ ] **Step 1: Skriv failing tests:**
  - `test_parse_links` (Review Focus 5), parametriserat:
    - `https://www.linkedin.com/jobs/view/4012345678/`
    - `https://www.linkedin.com/comm/jobs/view/4012345678?trackingId=x`
    - `https://se.linkedin.com/jobs/view/backend-developer-at-acme-4012345679`
    - en dubblett
    - raderna `hej` och `https://example.com/jobs/view/1`, som avvisas
    - blanka rader, som ignoreras
  - `test_too_many_links`: 21 unika id:n ger `TooManyLinks`.
  - `test_source_fetches_pages`: MockTransport med `job_page.html` för ett id och 999 för ett annat. Det första blir ett komplett jobb och det andra `incomplete` med titeln "LinkedIn-jobb <id>". Pausen anropas en gång.
  - `test_run.py::test_skip_filter_source_bypasses_hard_filter`: preferenser som skulle filtrera bort jobbet. Med `skip_filter=True` blir det `scored`.
  - `test_jobs.py::test_import_starts_run`, `test_import_in_progress_keeps_text`, `test_import_too_many`, `test_import_none_valid`, `test_import_shows_rejected_lines_escaped` och `test_home_has_import_box`.
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5:** Commit `feat: import LinkedIn jobs by pasting links`

---

### Task 10: Ihopkoppling, README och röktest

**Files:**
- Modify: `src/jobaplier/main.py`, `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: allt ovan
- Produces:
  - `build_runtime`:
    - `FormApplier(..., cv_pdf=lambda job, draft: build_cv_pdf(engine, paths, holder.get(), ai, draft.id, paths.data_dir / "tmp" / f"cv-{job.id}.pdf")[0])`
    - `AppState.cv_pdf = lambda draft_id, out: build_cv_pdf(engine, paths, holder.get(), ai, draft_id, out)`
    - `AppState.cv_document`, som läser via `load_or_build_cv_document` i en `session_scope` och fångar `CVMissing`, `CVUnreadable` och `AIError`, för vilka den returnerar `None`
    - `AppState.import_links = lambda ids: runner.start(sources=[LinkedInLinksSource(ids, http)])`, så att samma lås som "Sök nu" gäller
  - `Runner.start(sources: list[Source] | None = None)`: med `None` används som i dag de konfigurerade källorna, och annars bara de angivna. `execute_run` får motsvarande parameter.
  - README: avsnittet om Gmail-inställning tas bort, och nya avsnitt beskriver ansökningspaketet (mejl, formulär, okänt), det skräddarsydda CV:t (fakta kopieras från originalet, varningar, `/cv`) och import av LinkedIn-länkar.

- [ ] **Step 1: Skriv failing tests:**
  - `test_form_applier_gets_cv_provider`
  - `test_state_cv_pdf_wired`
  - `test_import_links_uses_runner_lock`: när en körning pågår kastas `RunInProgress`.
  - `test_import_links_runs_only_linkedin_source`
  - `test_cv_document_none_without_cv`
- [ ] **Step 2:** Kör testerna. Förväntat: FAIL.
- [ ] **Step 3:** Implementera och uppdatera README.
- [ ] **Step 4:** Hela sviten. Förväntat: PASS.
- [ ] **Step 5: Manuell röktest** med användarens riktiga `data/cv.pdf` och prenumeration, i en tillfällig datakatalog som kopia av `data/`:
  1. `serve`.
  2. Installationssidan listar inte Gmail.
  3. Importera en riktig LinkedIn-jobblänk via rutan.
  4. Klicka *Ansök* på jobbet.
  5. Ladda ner CV och brev från paketsidan.
  6. Läs ut CV-PDF:ens text och kontrollera att titlar och perioder matchar originalet.
  7. Klicka *Jag har skickat*.
  Inga formulär öppnas. Stoppa servern.
- [ ] **Step 6:** Commit `feat: wire tailored CV and LinkedIn import into the service`
