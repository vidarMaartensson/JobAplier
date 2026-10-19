# JobAplier – implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** En lokal Python-tjänst som regelbundet hämtar jobb från Platsbanken, LinkedIn-aviseringar och karriärsidor, poängsätter dem mot CV + preferenser med Claude, och låter användaren i en webbdashboard välja länk eller ansökan (utkast → godkänn → mejl skickas / formulär fylls i).

**Architecture:** En process: FastAPI-dashboard på `127.0.0.1:8000` + APScheduler i samma process, SQLite via SQLModel. Källor (`sources/`), pipeline (`pipeline/`) och ansökningssätt (`apply/`) är små moduler bakom protokoll så att de testas isolerat med falska klienter. Webblagret anropar bara `ApplyService` och `run_pipeline`. AI går som standard via Claude Code CLI (`claude -p`) och användarens Claude-prenumeration, med Anthropic-API:t som utbytbart alternativ bakom samma `AIClient`-gränssnitt.

**Tech Stack:** Python 3.12, uv, FastAPI, Jinja2, HTMX, SQLModel/SQLite, APScheduler 3.x, httpx, selectolax, pypdf, WeasyPrint, Playwright (sync), google-api-python-client + google-auth-oauthlib, Claude Code CLI (headless) / anthropic SDK, Pydantic v2, pytest, pytest-playwright.

**Spec:** `docs/superpowers/specs/2026-10-19-jobaplier-design.md`

## Global Constraints

- Python `>=3.12`, beroenden via `uv`. Paketet heter `jobaplier`, ligger i `src/jobaplier/`, konsolkommandot `jobaplier` pekar på `jobaplier.cli:main`.
- WeasyPrint kräver systembibliotek: `brew install pango` (dokumenteras i README, Task 1).
- Modeller, exakta id:n: poängsättning `claude-haiku-5-5`; personliga brev och CV-profil `claude-sonnet-5-5`.
- Kostnad räknas med (USD per 1M tokens in/ut): Haiku 5.5 `0.10 / 0.50`, Sonnet 5.5 `2.00 / 10.00`.
- AI-backend väljs med `ai.backend` (spec §3.2), standard `claude_code`.
  - `claude_code`: subprocess med exakt dessa argument (prompten på stdin, timeout 180 s, `cwd` = `paths.data_dir`): `[claude_bin, "-p", "--model", model, "--effort", effort, "--output-format", "json", "--json-schema", json.dumps(schema.model_json_schema()), "--system-prompt", system, "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]`. Svaret är ett JSON-objekt; validera `structured_output` med `schema.model_validate`. Fel: exit-kod ≠ 0, `is_error: true` eller `structured_output` saknas. `api_error_status == 429` betyder att prenumerationens användningsgräns är nådd. Använd aldrig `--bare` (kräver API-nyckel). Kostnad bokförs som `0.0`; tokens från `usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens` respektive `usage.output_tokens`.
  - `api`: `client.messages.parse(model=…, max_tokens=…, system=…, messages=[…], output_format=<PydanticModel>, output_config={"effort": …})`, resultatet läses från `response.parsed_output`. Kontrollera `response.stop_reason == "refusal"` innan `parsed_output` läses. Ingen assistant-prefill, ingen `thinking`-parameter, ingen `temperature`. Månadstaket gäller bara här.
- Dashboarden lyssnar bara på `127.0.0.1`, port `8000`.
- `data/` och `.env` är gitignorerade. Filer i `data/`: `jobaplier.db`, `cv.pdf`, `config.yaml`, `credentials.json`, `gmail_token.json`, `browser/`, `logs/`, `letter_examples/*.txt`, `eval/`.
- Standardvärden: schema `["08:00", "12:00", "17:00"]` (Europe/Stockholm), tröskel `60`, `dry_run: true`, `max_emails_per_day: 10`, `ai_monthly_budget_usd: 10`, första körningen hämtar `14` dagar bakåt.
- HTTP mot källor: timeout `30` s, upp till `3` försök med exponentiell backoff (1 s, 2 s) vid nätverksfel, 429 och 5xx. User-Agent `JobAplier/0.1 (personal use)`.
- Personligt brev: samma språk som annonsen (`sv`/`en`), `250–350` ord, endast fakta ur CV:t; påståenden utan stöd listas.
- Mejl: bilagor heter exakt `CV.pdf` och `Personligt_brev.pdf`. Gmail-scopes exakt `https://www.googleapis.com/auth/gmail.send` och `https://www.googleapis.com/auth/gmail.readonly`.
- Formulär: tjänsten klickar **aldrig** på Skicka/submit, löser aldrig CAPTCHA och kryssar aldrig i samtyckes-/villkorsrutor.
- Testsviten gör inga nätverksanrop och inga riktiga Anthropic-/Gmail-anrop.
- All text i dashboarden och i fel som visas för användaren är på svenska.
- **Tillägg till specens datamodell** (behövs för att uppfylla specen): `jobs.apply_reference` (annonsreferens för mejlämnet), `drafts.language`, tabellen `ai_calls` (månadskostnad måste inkludera brev, inte bara körningar), statusövergången `submitting → draft_ready` (spec §6: krasch i Playwright ger `draft_ready`), samt fältet `apply_email` i AI-poängsvaret (mejladress som bara står i annonstexten).

## Review Focus

1. **Samma jobb från två källor i samma körning** (Platsbanken + LinkedIn-avisering, båda nya) → ett `Job` med två `JobSource`, inte två rader i listan. Test i Task 9.
2. **Dubbelklick / två samtidiga "Godkänn och skicka"** → exakt ett mejl skickas, andra anropet får `AlreadyHandled`. Test i Task 15.
3. **Företags-/titelvarianter med svenska tecken och bolagsform** ("Exempel AB" vs "Exempel", "Utvecklare – Backend" vs "utvecklare backend", "Åkeri AB" ≠ "Akeri") → samma respektive olika dedupe-nyckel. Test i Task 9.
4. **Platsbanken-annons utan mejl och utan känd ATS-URL** → `method=unknown`, jobbet visas med länk till `webpage_url`, ingen Ansök-knapp. Test i Task 5 och Task 16.
5. **AI-fel för ett enskilt jobb mitt i en körning** (refusal eller ogiltigt svar två gånger) → det jobbet stannar som `new`, övriga poängsätts, körningen loggar felet. Test i Task 10.

---

## Filstruktur

```
pyproject.toml, README.md, config.example.yaml, .env.example
src/jobaplier/
  __init__.py
  config.py            # Config-modeller, Paths, load/save
  models.py            # SQLModel-tabeller + enums
  db.py                # engine, init, session_scope
  status.py            # tillåtna statusövergångar
  ai.py                # AIClient-protokoll, AnthropicAI, kostnad, budget
  gmail.py             # Gmail-protokoll, GoogleGmail, OAuth
  profile/cv.py        # CV-text, Profile, load_or_build_profile
  sources/base.py      # RawJob, ApplyInfo, Source, SourceError
  sources/http.py      # make_http_client, get_with_retry
  sources/ats.py       # detect_ats(url)
  sources/platsbanken.py
  sources/linkedin_alerts.py
  sources/careers/{__init__,greenhouse,lever,teamtailor}.py
  pipeline/dedupe.py   # dedupe_key, upsert_raw_jobs
  pipeline/filter.py   # hard_filter
  pipeline/scoring.py  # ScoreResult, score_job
  pipeline/run.py      # run_pipeline, RunInProgress
  apply/base.py        # Applier, SubmitResult, SubmitError
  apply/letter.py      # LetterResult, write_letter, load_examples
  apply/pdf.py         # render_letter_pdf
  apply/email.py       # EmailApplier
  apply/form/{__init__,fields,generic,applier,teamtailor,varbi,reachmee}.py
  apply/service.py     # ApplyService
  web/{app,state,routes_jobs,routes_drafts,routes_admin}.py
  web/templates/*.html, web/static/style.css
  scheduler.py, main.py, cli.py
launchd/se.jobaplier.plist.template
scripts/eval_prompts.py
tests/ (speglar src/, plus conftest.py, fakes.py, fixtures/)
```

---

### Task 1: Projektskelett och konfiguration

**Files:**
- Create: `pyproject.toml`, `README.md`, `config.example.yaml`, `.env.example`, `src/jobaplier/__init__.py`, `src/jobaplier/config.py`
- Test: `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `ProfileInfo(BaseModel)`: `name: str`, `email: str`, `phone: str = ""`, `linkedin_url: str = ""`
  - `Preferences(BaseModel)`: `roles`, `keywords`, `exclude_keywords`, `exclude_companies`, `locations`, `seniority` (alla `list[str] = []`), `remote_ok: bool = True`
  - `CareerSite(BaseModel)`: `type: Literal["greenhouse", "lever", "teamtailor"]`, `company: str`, `id: str`
  - `Toggle(BaseModel)`: `enabled: bool = True`
  - `SourcesConfig(BaseModel)`: `platsbanken: Toggle`, `linkedin_alerts: Toggle`, `careers: list[CareerSite] = []`
  - `ScoringConfig`: `threshold: int = 60`; `ApplyConfig`: `dry_run: bool = True`, `max_emails_per_day: int = 10`
  - `AIConfig`: `backend: Literal["claude_code", "api"] = "claude_code"`, `claude_bin: str = "claude"`
  - `Config(BaseModel)`: `profile: ProfileInfo`, `preferences`, `sources`, `scoring`, `schedule: list[str]`, `apply`, `ai: AIConfig`, `ai_monthly_budget_usd: float = 10.0` (standardvärden enligt Global Constraints)
  - `load_config(path: Path) -> Config`, `save_config(config: Config, path: Path) -> None`
  - `@dataclass(frozen=True) Paths` med fälten `data_dir, db, cv, config, gmail_credentials, gmail_token, browser, logs, letter_examples, eval_dir`; `Paths.from_data_dir(data_dir: Path) -> Paths`; `get_paths() -> Paths` (env `JOBAPLIER_DATA_DIR`, standard `./data`)
  - `anthropic_api_key() -> str | None` (läser `.env` via python-dotenv + miljön)
  - `tests/conftest.py`-fixtures: `paths` (Paths i `tmp_path`, kataloger skapade), `config` (minimal giltig `Config`), `make_pdf` (fabrik `make_pdf(text: str) -> Path` som renderar text till PDF med WeasyPrint)

- [ ] **Step 1: Skriv `pyproject.toml`** med beroendena från Tech Stack (+ `uvicorn`, `python-multipart`, `python-dotenv`, `pyyaml`, `tzdata`), dev-gruppen `pytest`, `pytest-playwright`, `[project.scripts] jobaplier = "jobaplier.cli:main"`, `[tool.pytest.ini_options] testpaths = ["tests"]`. Kör `uv sync && uv run playwright install chromium`. Förväntat: inga fel.

- [ ] **Step 2: Skriv failing tests** i `tests/test_config.py`:
  - `test_example_config_loads`: `load_config(Path("config.example.yaml"))` ger `Config` med `sources.careers[0].type == "teamtailor"`.
  - `test_defaults`: YAML med bara `profile: {name: "A", email: "a@x.se"}` → `scoring.threshold == 60`, `apply.dry_run is True`, `apply.max_emails_per_day == 10`, `schedule == ["08:00", "12:00", "17:00"]`, `ai_monthly_budget_usd == 10.0`, `ai.backend == "claude_code"`, `ai.claude_bin == "claude"`.
  - `test_invalid_backend_raises`: `ai: {backend: "openai"}` → `pydantic.ValidationError`.
  - `test_invalid_schedule_time_raises`: `schedule: ["25:00"]` → `pydantic.ValidationError`.
  - `test_save_roundtrip`: `save_config` + `load_config` ger lika objekt.
  - `test_paths_from_data_dir`: `Paths.from_data_dir(tmp).cv == tmp / "cv.pdf"`, `.gmail_token == tmp / "gmail_token.json"`.

- [ ] **Step 3: Kör** `uv run pytest tests/test_config.py -v`. Förväntat: FAIL (`ModuleNotFoundError: jobaplier.config`).

- [ ] **Step 4: Implementera `config.py`** enligt Interfaces. Schemavalidering: varje tid matchar `^([01]\d|2[0-3]):[0-5]\d$`. `config.example.yaml` = exemplet i spec §7. `.env.example` innehåller `ANTHROPIC_API_KEY=` med kommentaren att den bara behövs för `ai.backend: api`. README: förutsättningar (`brew install pango`, `uv sync`, `uv run playwright install chromium`, Claude Code installerat och inloggat med din prenumeration), placera `data/cv.pdf`, kopiera `config.example.yaml` till `data/config.yaml`.

- [ ] **Step 5: Kör** `uv run pytest tests/test_config.py -v`. Förväntat: PASS.

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: project skeleton and config loading"`

---

### Task 2: Datamodell, databas och statusövergångar

**Files:**
- Create: `src/jobaplier/models.py`, `src/jobaplier/db.py`, `src/jobaplier/status.py`
- Test: `tests/test_models.py`, `tests/test_status.py`; lägg fixture `engine` (SQLite i `tmp_path`, `init_db` körd) i `tests/conftest.py`

**Interfaces:**
- Produces:
  - Enums (`str, Enum`): `JobStatus` = `new, filtered, scored, dismissed, link, drafting, draft_ready, approved, submitting, submitted, failed`; `ApplyMethod` = `email, form, unknown`; `Channel` = `email, form, manual`; `ApplicationStatus` = `submitting, submitted, failed`
  - SQLModel-tabeller med fälten i spec §5 plus tilläggen: `Job` (+ `apply_reference: str | None`), `JobSource`, `ProfileRecord` (tabell `profiles`), `Draft` (+ `language: str`), `Application`, `Run`, `AICall` (tabell `ai_calls`: `id, at: datetime, model: str, purpose: str, input_tokens: int, output_tokens: int, cost_usd: float`). Fält som slutar på `_json` är `Column(JSON)`. Unika villkor: `jobs.dedupe_key`; `job_sources(source, external_id)`; `applications(job_id, channel)`.
  - `make_engine(db_path: Path) -> Engine` (`check_same_thread=False`, `PRAGMA journal_mode=WAL` och `foreign_keys=ON` vid connect), `init_db(engine) -> None`, `session_scope(engine) -> ContextManager[Session]` (commit vid lyckat block, rollback vid undantag)
  - `ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]]`, `class InvalidTransition(Exception)`, `transition(job: Job, to: JobStatus) -> None` (sätter `status` och `updated_at`, kastar `InvalidTransition` annars)

- [ ] **Step 1: Skriv failing tests.**
  - `tests/test_status.py::test_allowed_transitions` parametriserad över varje kant i spec §5.1 plus `submitting → draft_ready`: `transition` lyckas och `job.status == to`.
  - `test_disallowed_transitions` parametriserad över `(new, submitted)`, `(scored, approved)`, `(submitted, draft_ready)`, `(dismissed, drafting)`, `(filtered, scored)`: kastar `InvalidTransition`.
  - `tests/test_models.py::test_unique_dedupe_key`, `test_unique_source_external_id`, `test_unique_application_per_channel`: andra inserten kastar `sqlalchemy.exc.IntegrityError`.
  - `test_session_scope_rolls_back`: undantag i blocket → inga rader sparade.

- [ ] **Step 2: Kör** `uv run pytest tests/test_status.py tests/test_models.py -v`. Förväntat: FAIL (import).

- [ ] **Step 3: Implementera** `models.py`, `db.py`, `status.py` enligt Interfaces.

- [ ] **Step 4: Kör** samma kommando. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -am "feat: data model, db and status transitions"` (lägg till nya filer först).

---

### Task 3: AI-klient – Claude Code-backend, API-backend, kostnad och tak

**Files:**
- Create: `src/jobaplier/ai.py`, `tests/fakes.py`
- Test: `tests/test_ai.py`, `tests/test_ai_claude_code.py`

**Interfaces:**
- Consumes: `AICall`, `session_scope` (Task 2); `Config`, `Paths` (Task 1)
- Produces:
  - `SCORING_MODEL = "claude-haiku-5-5"`, `WRITING_MODEL = "claude-sonnet-5-5"`, `PRICES_PER_MTOK: dict[str, tuple[float, float]]`
  - `cost_usd(model: str, input_tokens: int, output_tokens: int) -> float`
  - `@dataclass AIResult(Generic[T])`: `value: T`, `cost_usd: float`
  - `class AIError(Exception)`, `class BudgetExceeded(AIError)` (månadstaket för `api` **eller** prenumerationens användningsgräns för `claude_code`; pipelinen behandlar båda likadant)
  - `class AIClient(Protocol)`: `complete(self, *, model: str, system: str, prompt: str, schema: type[T], purpose: str, max_tokens: int = 4096, effort: str = "medium") -> AIResult[T]`
  - `class ClaudeCodeAI`: `__init__(self, claude_bin: str, engine: Engine, cwd: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: float = 180.0)` (`max_tokens` ignoreras; CLI:n styr själv utdatalängden)
  - `class AnthropicAI`: `__init__(self, client: anthropic.Anthropic, engine: Engine, monthly_limit_usd: float)`
  - `make_ai(config: Config, engine: Engine, paths: Paths) -> AIClient` (väljer efter `config.ai.backend`)
  - `claude_code_available(claude_bin: str) -> bool` (`shutil.which`; används av installationssidan i Task 17)
  - `monthly_spend(engine: Engine, now: datetime | None = None) -> float` (summa `ai_calls.cost_usd` innevarande kalendermånad)
  - `tests/fakes.py::FakeAI(AIClient)`: `__init__(self, responses: dict[type, list[Any] | Callable[[str], Any]])`; ett köat `Exception`-objekt kastas i stället för att returneras; `calls: list[dict]` sparar `model, system, prompt, schema, purpose`; `cost_usd` per anrop `0.001`.

- [ ] **Step 1a: Skriv failing tests för `ClaudeCodeAI`** i `tests/test_ai_claude_code.py` med en falsk `run` som sparar argumenten och returnerar `subprocess.CompletedProcess(args, returncode, stdout=json.dumps(payload), stderr="")`. `payload` har formen som provkördes 2026-10-19: `{"type": "result", "subtype": "success", "is_error": false, "api_error_status": null, "structured_output": {...}, "result": "...", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1417, "cache_read_input_tokens": 0, "output_tokens": 30}}`.
  - `test_builds_exact_command`: argumentlistan är exakt den i Global Constraints, med `--model claude-haiku-5-5`, `--effort low` och schemat som JSON; `input` (stdin) är prompten; `cwd` och `timeout=180.0` skickas vidare; `capture_output=True`, `text=True`.
  - `test_returns_validated_structured_output`: `value` är en schemainstans; `cost_usd == 0.0`; en `AICall`-rad med `input_tokens == 1419` och `output_tokens == 30`.
  - `test_retries_once_on_missing_structured_output`: första svaret saknar `structured_output`, andra är giltigt → lyckas, två anrop.
  - `test_raises_after_two_invalid`: `structured_output` som inte validerar mot schemat två gånger → `AIError`.
  - `test_nonzero_exit_raises`: `returncode=1`, `stderr="not logged in"` → `AIError` vars meddelande innehåller `"not logged in"`.
  - `test_is_error_raises`: `is_error: true` → `AIError`.
  - `test_usage_limit_raises_budget_exceeded`: `is_error: true`, `api_error_status: 429` → `BudgetExceeded` med meddelandet `"Claude-prenumerationens användningsgräns är nådd"`.
  - `test_timeout_raises`: `run` kastar `subprocess.TimeoutExpired` → `AIError`.
  - `test_missing_binary_raises`: `run` kastar `FileNotFoundError` → `AIError` vars meddelande innehåller `"Claude Code"`.
  - `test_make_ai_selects_backend`: `backend="claude_code"` → `ClaudeCodeAI`; `backend="api"` → `AnthropicAI`.

- [ ] **Step 1b: Skriv failing tests för `AnthropicAI`** i `tests/test_ai.py` med en stub för `anthropic.Anthropic` (objekt med `.messages.parse(**kw)` som returnerar `SimpleNamespace(parsed_output=…, stop_reason="end_turn", usage=SimpleNamespace(input_tokens=…, output_tokens=…))`):
  - `test_cost_usd`: `cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)`; `cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)`.
  - `test_complete_returns_parsed_and_records_call`: resultatet har `value` = stubbens `parsed_output`; en `AICall`-rad med rätt `purpose`, tokens och kostnad.
  - `test_passes_schema_and_effort`: stubben fick `output_format=Schema`, `output_config={"effort": "low"}`, `model`, `system`.
  - `test_retries_once_when_parsed_output_none`: första svaret `parsed_output=None`, andra giltigt → lyckas, två anrop.
  - `test_raises_after_two_invalid`: två `None` → `AIError`.
  - `test_refusal_raises`: `stop_reason="refusal"` → `AIError`, inget nytt försök.
  - `test_budget_blocks_call`: `AICall` på 10.0 USD i månaden, gräns 10.0 → `BudgetExceeded`, stubben anropas inte.
  - `test_api_errors_wrapped`: stubben kastar `anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))` → `AIError`.

- [ ] **Step 2: Kör** `uv run pytest tests/test_ai.py tests/test_ai_claude_code.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera `ai.py`.** `ClaudeCodeAI` gör ett nytt försök vid saknat eller ogiltigt `structured_output`, men inte vid exit-fel, timeout eller 429. `AnthropicAI` kontrollerar budgeten före varje anrop. Fånga `anthropic.APIStatusError` och `anthropic.APIConnectionError` (SDK:t gör redan egna nya försök; skapa klienten i `main.py` med `max_retries=4`) och kasta `AIError` med statuskod/meddelande. Skriv `FakeAI`.

- [ ] **Step 4: Kör** `uv run pytest tests/test_ai.py tests/test_ai_claude_code.py -v`. Förväntat: PASS.

- [ ] **Step 5: Manuell kontroll mot riktig CLI** (inte en del av testsviten): kör ett kort skript utanför repot som skapar `ClaudeCodeAI("claude", make_engine(tmp / "t.db"), cwd=tmp)` (efter `init_db`) och anropar `complete(model=SCORING_MODEL, system="Svara kort.", prompt="Ge poäng 42.", schema=Score, purpose="smoke", effort="low")` där `class Score(BaseModel): score: int`. Förväntat: `value.score == 42` inom några sekunder.

- [ ] **Step 6: Commit** `git commit -m "feat: AI clients for Claude Code subscription and API"`

---

### Task 4: CV och profil

**Files:**
- Create: `src/jobaplier/profile/__init__.py`, `src/jobaplier/profile/cv.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: `AIClient`, `WRITING_MODEL` (Task 3); `ProfileRecord` (Task 2)
- Produces:
  - `Profile(BaseModel)`: `summary: str`, `skills: list[str]`, `years_experience: float`, `roles: list[str]`, `languages: list[str]`, `education: list[str]`
  - `class CVMissing(Exception)`, `class CVUnreadable(Exception)`
  - `extract_cv_text(pdf: Path) -> str`
  - `load_or_build_profile(session: Session, cv_path: Path, ai: AIClient) -> ProfileRecord`
  - `profile_of(record: ProfileRecord) -> Profile` (validerar `record.profile_json`; håller `models.py` fritt från Pydantic-scheman)

- [ ] **Step 1: Skriv failing tests:**
  - `test_extract_text`: `make_pdf("Python-utvecklare med 5 års erfarenhet")` → texten innehåller `"Python-utvecklare"`.
  - `test_builds_profile_on_first_run`: `FakeAI` med en `Profile` → en `ProfileRecord` med `cv_hash` = sha256 av filens bytes; AI-anropet har `model == "claude-sonnet-5-5"`, `purpose == "profile"`, och prompten innehåller CV-texten.
  - `test_reuses_profile_when_cv_unchanged`: andra anropet gör inget AI-anrop och returnerar samma rad.
  - `test_rebuilds_when_cv_changed`: ny PDF-text → ny rad, två AI-anrop totalt.
  - `test_missing_cv_raises`: `CVMissing`.
  - `test_empty_text_raises`: PDF utan text (`make_pdf("")`) → `CVUnreadable`.

- [ ] **Step 2: Kör** `uv run pytest tests/test_profile.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera** med pypdf. Systemprompt: extrahera endast det som står i CV:t; tomma listor om uppgift saknas.

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: CV text extraction and cached profile"`

---

### Task 5: Källbas, HTTP-hjälp, ATS-igenkänning och Platsbanken

**Files:**
- Create: `src/jobaplier/sources/__init__.py`, `sources/base.py`, `sources/http.py`, `sources/ats.py`, `sources/platsbanken.py`
- Test: `tests/sources/test_http.py`, `tests/sources/test_ats.py`, `tests/sources/test_platsbanken.py`, `tests/fixtures/platsbanken/page1.json`, `page2.json`

**Interfaces:**
- Consumes: `Preferences` (Task 1), `ApplyMethod` (Task 2)
- Produces:
  - `@dataclass ApplyInfo`: `method: ApplyMethod`, `email: str | None = None`, `form_url: str | None = None`, `reference: str | None = None`
  - `@dataclass RawJob`: `source: str`, `external_id: str`, `title: str`, `company: str`, `location: str | None`, `remote: bool | None`, `description: str`, `url: str`, `published_at: datetime | None`, `apply: ApplyInfo`, `incomplete: bool = False`
  - `class Source(Protocol)`: `name: str`; `fetch(self, since: datetime) -> list[RawJob]`
  - `class SourceError(Exception)`
  - `make_http_client() -> httpx.Client`; `get_with_retry(client: httpx.Client, url: str, *, params: dict | None = None, attempts: int = 3, base_delay: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> httpx.Response`
  - `detect_ats(url: str) -> str | None` returnerar `"teamtailor" | "varbi" | "reachmee" | "greenhouse" | "lever" | "workday"` utifrån domän (`*.teamtailor.com`, `*.varbi.com`, `*.reachmee.com`/`site*.reachmee.com`, `boards.greenhouse.io`/`job-boards.greenhouse.io`, `jobs.lever.co`, `*.myworkdayjobs.com`)
  - `PlatsbankenSource(prefs: Preferences, http: httpx.Client)`, `name = "platsbanken"`

- [ ] **Step 1: Skriv fixtures** enligt JobTech JobSearch-svarets form (`total.value`, `hits[]` med `id, headline, employer.name, workplace_address.municipality, workplace_address.region, description.text, webpage_url, publication_date, application_details.{email,url,reference}`). Kontrollera fältnamnen en gång mot https://jobsearch.api.jobtechdev.se (Swagger) innan fixturen skrivs. `page1.json`: `total.value = 150`, 100 träffar där minst en har mejl, en har Teamtailor-URL, en har varken mejl eller URL, en har okänd URL. `page2.json`: 50 träffar.

- [ ] **Step 2: Skriv failing tests** (httpx `MockTransport`, ingen nätverkstrafik):
  - `test_http.py::test_retries_then_succeeds`: 503, 503, 200 → svaret 200, `sleep` anropad med `1.0` och `2.0`.
  - `test_http.py::test_raises_source_error_after_attempts`: tre 503 → `SourceError`.
  - `test_http.py::test_no_retry_on_404`: 404 → `SourceError` efter ett anrop.
  - `test_ats.py` parametriserad: `https://exempel.teamtailor.com/jobs/123` → `"teamtailor"`, `https://foo.varbi.com/se/what:job/jobID:1` → `"varbi"`, `https://web103.reachmee.com/ext/I003/...` → `"reachmee"`, `https://exempel.se/karriar` → `None`.
  - `test_platsbanken.py::test_maps_fields`: första träffen ger rätt `title, company, location, description, url, published_at`, `source == "platsbanken"`, `external_id == hit["id"]`.
  - `test_email_apply`: träff med mejl → `ApplyInfo(method=email, email=…, reference=…)`.
  - `test_ats_url_is_form`: Teamtailor-URL → `method=form`, `form_url` satt.
  - `test_no_apply_info_is_unknown` (Review Focus 4): varken mejl eller känd ATS-URL → `method=unknown`, `url == webpage_url`.
  - `test_paging_and_since`: anrop med `offset=0` och `offset=100`, `limit=100`, `published-after == since.strftime("%Y-%m-%dT%H:%M:%S")`; stannar när `offset >= total` (och aldrig `offset > 2000`).
  - `test_one_query_per_role_merged`: `roles=["backend", "fullstack"]` → två frågor (`q=backend`, `q=fullstack`), samma `id` i båda ger ett `RawJob`.

- [ ] **Step 3: Kör** `uv run pytest tests/sources -v`. Förväntat: FAIL.

- [ ] **Step 4: Implementera.** `PlatsbankenSource.fetch` gör en fråga per `prefs.roles` (eller en fråga per `keywords` om `roles` är tomt), endpoint `https://jobsearch.api.jobtechdev.se/search`. `remote` sätts till `None`. `location` = kommun, annars region.

- [ ] **Step 5: Kör** testet. Förväntat: PASS.

- [ ] **Step 6: Commit** `git commit -m "feat: source base, http retry, ATS detection, Platsbanken"`

---

### Task 6: Karriärsidor (Greenhouse, Lever, Teamtailor)

**Files:**
- Create: `src/jobaplier/sources/careers/__init__.py`, `greenhouse.py`, `lever.py`, `teamtailor.py`
- Test: `tests/sources/test_careers.py`, `tests/fixtures/careers/greenhouse.json`, `lever.json`, `teamtailor.rss`

**Interfaces:**
- Consumes: `RawJob`, `ApplyInfo`, `Source`, `get_with_retry` (Task 5), `CareerSite` (Task 1)
- Produces: `GreenhouseSource(site: CareerSite, http)`, `LeverSource(site, http)`, `TeamtailorSource(site, http)` med `name` = `"careers:greenhouse:<id>"` osv.; `build_career_sources(sites: list[CareerSite], http: httpx.Client) -> list[Source]`; `html_to_text(html: str) -> str` (i `careers/__init__.py`, används även av Task 8)

Endpoints:
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{id}/jobs?content=true` → `jobs[]` (`id, title, location.name, absolute_url, updated_at, content` – HTML-escapad HTML).
- Lever: `https://api.lever.co/v0/postings/{id}?mode=json` → lista (`id, text, categories.location, hostedUrl, applyUrl, createdAt` i ms, `descriptionPlain`, `workplaceType`).
- Teamtailor: `https://{id}.teamtailor.com/jobs.rss`; om `id` innehåller en punkt tolkas det som egen domän → `https://{id}/jobs.rss`. Kontrollera flödets adress och fält mot en riktig Teamtailor-karriärsida innan fixturen skrivs; om adressen skiljer sig, ändra bara `FEED_URL`.

- [ ] **Step 1: Skriv failing tests:**
  - `test_greenhouse_maps_and_unescapes`: beskrivningen innehåller ingen `<` eller `&lt;`; `apply.method == form`, `form_url == absolute_url`; `company == site.company`.
  - `test_lever_remote_and_apply_url`: `workplaceType == "remote"` → `remote is True`; `form_url == applyUrl`; `published_at` från `createdAt` ms.
  - `test_teamtailor_parses_items`: titel, länk, `pubDate`, beskrivning som text.
  - `test_since_filters_old`: jobb publicerade/uppdaterade före `since` utesluts (för alla tre).
  - `test_build_career_sources`: tre sajter av olika typ → tre källor av rätt klass.

- [ ] **Step 2: Kör** `uv run pytest tests/sources/test_careers.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera** (selectolax för HTML→text, `xml.etree.ElementTree` för RSS).

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: Greenhouse, Lever and Teamtailor career sources"`

---

### Task 7: Gmail-klient

**Files:**
- Create: `src/jobaplier/gmail.py`
- Modify: `tests/fakes.py` (lägg till `FakeGmail`)
- Test: `tests/test_gmail.py`, `tests/fixtures/gmail/message_multipart.json`

**Interfaces:**
- Produces:
  - `SCOPES: list[str]` (exakt enligt Global Constraints)
  - `@dataclass Mail`: `id: str`, `sender: str`, `subject: str`, `received_at: datetime`, `html: str`, `text: str`
  - `class GmailAuthError(Exception)`
  - `class Gmail(Protocol)`: `search(self, query: str) -> list[Mail]`; `send(self, msg: email.message.EmailMessage) -> str` (returnerar Gmails message-id)
  - `class GoogleGmail(Gmail)`: `from_files(credentials: Path, token: Path) -> GoogleGmail` (laddar token, förnyar vid behov, sparar förnyad token; kastar `GmailAuthError` om token saknas eller inte går att förnya – öppnar **aldrig** en webbläsare)
  - `parse_message(payload: dict) -> Mail` (Gmail API `users.messages.get(format="full")`, base64url-delar, multipart)
  - `run_oauth_flow(credentials: Path, token: Path) -> None` (`InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)`)
  - `FakeGmail`: `__init__(self, mails: list[Mail] = [], fail_send: Exception | None = None, send_delay: float = 0.0)`; `sent: list[EmailMessage]`; `queries: list[str]`; `send` returnerar `f"fake-{n}"`

- [ ] **Step 1: Skriv failing tests:**
  - `test_parse_multipart_message`: fixturen ger rätt `sender`, `subject`, `received_at` (från `internalDate`), både `html` och `text`.
  - `test_from_files_without_token_raises`: `GmailAuthError`.
  - `test_from_files_invalid_token_raises`: token-fil med utgången token utan refresh_token → `GmailAuthError`.

- [ ] **Step 2: Kör** `uv run pytest tests/test_gmail.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.** `GoogleGmail.search` hämtar id-listan med `users.messages.list(userId="me", q=query)` (alla sidor) och sedan varje meddelande med `format="full"`. `send` skickar `{"raw": base64url(msg.as_bytes())}`.

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: Gmail client wrapper and OAuth flow"`

---

### Task 8: LinkedIn-aviseringar

**Files:**
- Create: `src/jobaplier/sources/linkedin_alerts.py`
- Test: `tests/sources/test_linkedin_alerts.py`, `tests/fixtures/linkedin/alert.html`, `tests/fixtures/linkedin/job_page.html`

**Interfaces:**
- Consumes: `Gmail`, `Mail`, `GmailAuthError` (Task 7); `RawJob`, `SourceError`, `get_with_retry` (Task 5); `html_to_text` (Task 6)
- Produces: `LinkedInAlertsSource(gmail: Gmail, http: httpx.Client, sleep: Callable[[float], None] = time.sleep)`, `name = "linkedin_alerts"`; `extract_job_refs(html: str) -> list[tuple[str, str, str]]` (job-id, titel, företag ur mejlet, unika i ordning)

Regler:
- Gmail-fråga: `from:jobalerts-noreply@linkedin.com after:{int(since.timestamp())}`.
- Jobb-id ur länkar som matchar `linkedin\.com/(?:comm/)?jobs/view/(\d+)`.
- Publik sida `https://www.linkedin.com/jobs/view/{id}`: titel `h1.top-card-layout__title`, företag `a.topcard__org-name-link`, ort `span.topcard__flavor--bullet`, beskrivning `div.show-more-less-html__markup`. Vänta 1 s mellan sidhämtningar.
- Om sidan inte kan hämtas (annan status än 200, t.ex. 999, eller `SourceError`) eller saknar titel → `incomplete=True` med titel och företag från mejlet.
- `apply.method = unknown`, `url` = kanonisk `https://www.linkedin.com/jobs/view/{id}`.
- `GmailAuthError` → `SourceError("Gmail-inloggningen har gått ut – kör `jobaplier gmail-login`")`.

- [ ] **Step 1: Skriv fixtures.** `alert.html` sparas från ett riktigt LinkedIn-aviseringsmejl (personuppgifter bortplockade) och innehåller samma jobb två gånger. `job_page.html` sparas från en publik jobbsida.

- [ ] **Step 2: Skriv failing tests:**
  - `test_extract_unique_refs`: dubblerat jobb i mejlet → ett id.
  - `test_fetches_public_page`: `FakeGmail` med ett mejl + `MockTransport` som returnerar `job_page.html` → `RawJob` med beskrivning, `incomplete is False`, `source == "linkedin_alerts"`.
  - `test_blocked_page_gives_incomplete`: status 999 → `incomplete is True`, titel från mejlet.
  - `test_query_uses_since`: `FakeGmail.queries[0]` innehåller `after:<epoch>`.
  - `test_auth_error_becomes_source_error`: meddelandet innehåller `jobaplier gmail-login`.

- [ ] **Step 3: Kör** `uv run pytest tests/sources/test_linkedin_alerts.py -v`. Förväntat: FAIL.

- [ ] **Step 4: Implementera.**

- [ ] **Step 5: Kör** testet. Förväntat: PASS.

- [ ] **Step 6: Commit** `git commit -m "feat: LinkedIn job alerts source via Gmail"`

---

### Task 9: Dubbletter och hårt filter

**Files:**
- Create: `src/jobaplier/pipeline/__init__.py`, `pipeline/dedupe.py`, `pipeline/filter.py`
- Test: `tests/pipeline/test_dedupe.py`, `tests/pipeline/test_filter.py`

**Interfaces:**
- Consumes: `RawJob` (Task 5), `Job`, `JobSource`, `ApplyMethod`, `JobStatus` (Task 2), `Preferences` (Task 1)
- Produces:
  - `normalize(text: str) -> str`; `dedupe_key(company: str, title: str) -> str` (= `f"{normalize(company)}|{normalize(title)}"`)
  - `upsert_raw_jobs(session: Session, raws: list[RawJob]) -> list[Job]` (returnerar endast **nya** jobb, status `new`)
  - `hard_filter(job: Job, prefs: Preferences) -> str | None` (returnerar `filter_reason` eller `None`)

Normalisering: `casefold`; alla tecken som inte är bokstäver/siffror (inkl. `–`, `-`, `/`, `(`, `)`) blir mellanslag; ta bort token i `{"ab", "aktiebolag", "publ", "ltd", "limited", "inc", "gmbh", "as", "asa", "oy", "oyj", "aps", "llc"}` ur företagsnamnet; komprimera mellanslag. `å ä ö` behålls (ingen ASCII-folding).

Upsert: finns `(source, external_id)` → hoppa över. Annars: finns `dedupe_key` → lägg till `JobSource` på befintligt jobb och uppgradera ansökningsväg om den nya är bättre (`email` > `form` > `unknown`, inkl. `apply_email`/`apply_form_url`/`apply_reference`). Annars nytt `Job`. Dubbletter **inom samma batch** hanteras likadant (flush efter varje nytt jobb).

Filterregler i ordning, svenska skäl:
1. `normalize(company)` finns bland normaliserade `exclude_companies` → `"Uteslutet företag: {company}"`.
2. Något `exclude_keywords` (casefold) finns i titeln → `"Uteslutet nyckelord: {kw}"`.
3. `locations` icke-tom, `job.location` inte `None`, ingen ort i `locations` finns (casefold, delsträng) i `job.location`, och inte (`remote_ok` och `job.remote is True`) → `"Ort matchar inte: {location}"`. Okänd ort släpps igenom.
4. `seniority` icke-tom och titeln innehåller en markör för en nivå som inte finns i `seniority` → `"Senioritet: {nivå}"`. Markörer: `junior: ["junior"]`, `senior: ["senior", "sr."]`, `lead: ["lead", "principal", "staff", "head of"]`; `mid` har inga markörer.

- [ ] **Step 1: Skriv failing tests:**
  - `test_dedupe.py::test_key_variants` (Review Focus 3): `dedupe_key("Exempel AB", "Utvecklare – Backend") == dedupe_key("exempel", "utvecklare backend")`; `dedupe_key("Åkeri AB", "X") != dedupe_key("Akeri", "X")`.
  - `test_same_job_two_sources_same_batch` (Review Focus 1): ett Platsbanken-`RawJob` (mejl) och ett LinkedIn-`RawJob` (unknown) med samma företag/titel i **samma** anrop → ett `Job`, två `JobSource`, `apply_method == email`, returnerad lista har längd 1.
  - `test_upgrades_apply_method_later_run`: först `unknown`, senare körning ger `form` → `apply_method == form`; inget nytt jobb returneras.
  - `test_skips_known_source_id`: samma `(source, external_id)` två gånger → inga nya rader.
  - `test_filter.py`: ett test per regel med förväntad skälsträng; `test_unknown_location_passes`; `test_remote_overrides_location`; `test_mid_only_drops_senior`: `seniority=["junior", "mid"]`, titel "Senior Backend Developer" → `"Senioritet: senior"`.

- [ ] **Step 2: Kör** `uv run pytest tests/pipeline -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: job dedupe and preference hard filter"`

---

### Task 10: AI-poäng och sökkörningen

**Files:**
- Create: `src/jobaplier/pipeline/scoring.py`, `pipeline/run.py`
- Test: `tests/pipeline/test_scoring.py`, `tests/pipeline/test_run.py`

**Interfaces:**
- Consumes: `AIClient`, `AIError`, `BudgetExceeded`, `SCORING_MODEL` (Task 3); `Profile`, `load_or_build_profile`, `profile_of` (Task 4); `Source`, `SourceError` (Task 5); `upsert_raw_jobs`, `hard_filter` (Task 9); `transition` (Task 2)
- Produces:
  - `ScoreResult(BaseModel)`: `score: int = Field(ge=0, le=100)`, `reasons: str`, `apply_method_hint: Literal["email", "form", "unknown"]`, `apply_email: str | None = None`
  - `score_job(job: Job, profile: Profile, prefs: Preferences, ai: AIClient) -> AIResult[ScoreResult]` (`model=SCORING_MODEL`, `effort="low"`, `purpose="scoring"`, `max_tokens=1024`)
  - `run_pipeline(engine: Engine, sources: list[Source], prefs: Preferences, ai: AIClient, cv_path: Path, *, trigger: Literal["schedule", "manual"], now: Callable[[], datetime] = ...) -> Run`
  - `class RunInProgress(Exception)`
  - `Run.stats_json` får formen `{"sources": {name: {"ok": bool, "count": int}}, "new": int, "filtered": int, "scored": int, "scoring_failed": int}`; `Run.errors_json` = `{name_or_"scoring"_or_"budget": "meddelande"}`

Körningens ordning: ta modulens `threading.Lock` med `acquire(blocking=False)`, annars `RunInProgress` → skapa `Run` → profil (`CVMissing`/`CVUnreadable` → fel i `errors_json["profile"]`, körningen avslutas utan poängsättning) → för varje källa: `since` = `started_at` för senaste `Run` där `stats_json["sources"][name]["ok"]` är sant, annars `now() - 14 dagar`; `fetch` i try/except (`SourceError` och övriga `Exception`) → `upsert_raw_jobs` → `hard_filter` på nya jobb (`new → filtered` med skäl) → poängsätt **alla** jobb med status `new` (även kvarlämnade från tidigare körningar): lyckat → fyll `score`, `score_reasons`; om `apply_method == unknown` och `apply_email` finns → sätt `apply_method = email`, `apply_email`; `new → scored`. `AIError` → jobbet stannar `new`, räkna `scoring_failed`, spara senaste felet i `errors_json["scoring"]`. `BudgetExceeded` → avbryt poängsättningen, `errors_json["budget"] = str(e)` (t.ex. "AI-budgeten för månaden är slut" eller "Claude-prenumerationens användningsgräns är nådd"). Sätt `finished_at`, `ai_cost_usd` (summan av körningens `AIResult.cost_usd`). Prompten säger att `reasons` ska vara 2–3 meningar på svenska; för `incomplete` jobb står det att bara titel och företag är kända.

- [ ] **Step 1: Skriv failing tests** (`FakeAI`, falska `Source`-klasser i testfilen):
  - `test_scoring.py::test_prompt_contains_profile_prefs_and_ad`, `test_uses_haiku_low_effort`.
  - `test_run.py::test_happy_path`: två källor, tre jobb, ett filtreras → statusar `scored, scored, filtered`; `Run.stats_json["scored"] == 2`.
  - `test_failing_source_does_not_stop_others`: en källa kastar `SourceError("x")` → den andra källans jobb poängsätts; `errors_json["<namn>"] == "x"`, `sources[name]["ok"] is False`.
  - `test_ai_error_for_one_job_keeps_it_new` (Review Focus 5): `FakeAI` köar `[ScoreResult(...), AIError("refusal"), ScoreResult(...)]` → statusar `scored, new, scored`; `stats["scoring_failed"] == 1`; nästa körning med lyckat svar poängsätter det kvarlämnade jobbet.
  - `test_budget_stops_scoring`: `BudgetExceeded` på första jobbet → inga jobb `scored`, `errors_json["budget"]` satt.
  - `test_since_per_source`: källa som lyckades i förra körningen får `since == förra.started_at`; källa som misslyckades får `now - 14 dagar`.
  - `test_concurrent_run_raises`: lås hållet → `RunInProgress`.
  - `test_ai_found_email_upgrades_method`: `apply_email="hr@x.se"` på `unknown`-jobb → `apply_method == email`.
  - `test_missing_cv_recorded`: ingen CV-fil → `errors_json["profile"]` satt, inga AI-anrop.

- [ ] **Step 2: Kör** `uv run pytest tests/pipeline -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: AI scoring and pipeline run orchestration"`

---

### Task 11: Personligt brev och PDF

**Files:**
- Create: `src/jobaplier/apply/__init__.py`, `apply/letter.py`, `apply/pdf.py`
- Test: `tests/apply/test_letter.py`, `tests/apply/test_pdf.py`

**Interfaces:**
- Consumes: `AIClient`, `WRITING_MODEL` (Task 3); `ProfileRecord`, `profile_of` (Task 4); `Job` (Task 2)
- Produces:
  - `LetterResult(BaseModel)`: `language: Literal["sv", "en"]`, `letter: str`, `unsupported_claims: list[str]`
  - `load_examples(letter_examples_dir: Path) -> list[str]` (högst 2 `.txt`-filer i namnordning; tom lista om katalogen saknas)
  - `write_letter(job: Job, profile: ProfileRecord, ai: AIClient, *, examples: list[str], instruction: str | None = None, previous: str | None = None) -> AIResult[LetterResult]` (`model=WRITING_MODEL`, `effort="medium"`, `purpose="letter"`, `max_tokens=4096`)
  - `word_count(text: str) -> int`
  - `render_letter_pdf(letter: str, applicant_name: str, out: Path) -> Path` (WeasyPrint; namn överst, dagens datum, ett `<p>` per stycke; typsnitt sans-serif 11pt, marginal 2.5 cm)

Prompten innehåller annonsens titel, företag och beskrivning, profilen, hela CV-texten och stilexemplen, samt reglerna i Global Constraints (språk, 250–350 ord, endast CV-fakta, lista påståenden utan stöd i `unsupported_claims`). Vid ny version läggs `previous` och `instruction` till.

- [ ] **Step 1: Skriv failing tests:**
  - `test_prompt_contains_cv_ad_and_rules`: prompten innehåller CV-texten, annonsbeskrivningen och `"250–350"`; `model == "claude-sonnet-5-5"`.
  - `test_regenerate_includes_previous_and_instruction`.
  - `test_examples_included_and_capped`: tre exempelfiler → bara två i prompten.
  - `test_word_count`: `word_count("Hej där, världen!") == 3`.
  - `test_pdf.py::test_renders_pdf`: filen börjar med `%PDF`, och `extract_cv_text` (Task 4) på den innehåller brevets första mening och sökandens namn.

- [ ] **Step 2: Kör** `uv run pytest tests/apply/test_letter.py tests/apply/test_pdf.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: cover letter writer and PDF rendering"`

---

### Task 12: Mejlansökan

**Files:**
- Create: `src/jobaplier/apply/base.py`, `apply/email.py`
- Test: `tests/apply/test_email.py`

**Interfaces:**
- Consumes: `Gmail` (Task 7); `render_letter_pdf` (Task 11); `Config`, `Paths` (Task 1); `Job`, `Draft`, `Channel`, `ApplicationStatus` (Task 2)
- Produces:
  - `@dataclass SubmitResult`: `channel: Channel`, `status: ApplicationStatus`, `receipt: str | None = None`, `dry_run: bool = False`, `notes: list[str] = field(default_factory=list)`
  - `class SubmitError(Exception)`, `class DailyLimitReached(SubmitError)`
  - `class Applier(Protocol)`: `submit(self, job: Job, draft: Draft) -> SubmitResult`
  - `EmailApplier(gmail: Gmail, config: Callable[[], Config], paths: Paths, sent_today: Callable[[], int])`
  - `build_application_email(job: Job, draft: Draft, config: Config, paths: Paths, *, to: str, subject: str, letter_pdf: Path) -> EmailMessage`

Regler:
- Ämne: `f"Ansökan: {job.title}"`, och om `job.apply_reference` finns: `f"Ansökan: {job.title} (ref. {job.apply_reference})"`.
- Torrkörning (`config.apply.dry_run`): mottagare `config.profile.email`, ämnet får prefixet `f"[TORRKÖRNING → {riktig mottagare}] "`; `SubmitResult.dry_run = True`.
- Brödtext efter `draft.language`:
  - `sv`: `"Hej!\n\nHär kommer min ansökan till tjänsten {title}. CV och personligt brev bifogas.\n\nVänliga hälsningar\n{name}\n{phone}"`
  - `en`: `"Hello,\n\nPlease find attached my application for the {title} position, including my CV and cover letter.\n\nBest regards\n{name}\n{phone}"`
- Bilagor: `paths.cv` som `CV.pdf` och renderat brev som `Personligt_brev.pdf`, båda `application/pdf`.
- Dagstak: om inte torrkörning och `sent_today() >= max_emails_per_day` → `DailyLimitReached("Dagens tak på {n} mejlansökningar är nått")` **innan** något skickas.
- Fel från `gmail.send` → `SubmitError(str(e))`.

- [ ] **Step 1: Skriv failing tests** med `FakeGmail`: `test_recipient_subject_body`, `test_subject_with_reference`, `test_attachments`, `test_dry_run_redirects_to_self`, `test_daily_limit_blocks_before_send` (inget i `FakeGmail.sent`), `test_dry_run_ignores_limit`, `test_gmail_error_becomes_submit_error`, `test_english_body`.

- [ ] **Step 2: Kör** `uv run pytest tests/apply/test_email.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera** (brevets PDF renderas till en temporär fil).

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: email applier with dry run and daily cap"`

---

### Task 13: Formulärifyllning – generell adapter och FormApplier

**Files:**
- Create: `src/jobaplier/apply/form/__init__.py`, `apply/form/fields.py`, `apply/form/generic.py`, `apply/form/applier.py`
- Test: `tests/apply/form/test_generic.py`, `tests/apply/form/test_applier.py`, `tests/fixtures/forms/unknown.html`

**Interfaces:**
- Consumes: `Applier`, `SubmitResult`, `SubmitError` (Task 12); `render_letter_pdf` (Task 11); `detect_ats` (Task 5); `Config`, `Paths` (Task 1)
- Produces:
  - `@dataclass Applicant`: `first_name, last_name, email, phone, linkedin_url: str`, `cv_path: Path`, `letter_text: str`, `letter_pdf: Path`; `Applicant.from_config(config, paths, letter_text, letter_pdf)` (delar `profile.name` på första mellanslaget)
  - `FIELD_PATTERNS: dict[str, list[str]]` (regex, casefold, mot etikett/`name`/`id`/`placeholder`/`aria-label`):
    `first_name: ["förnamn", "first.?name", "given.?name"]`, `last_name: ["efternamn", "last.?name", "surname", "family.?name"]`, `full_name: ["^namn$", "full.?name", "^name$", "ditt namn"]`, `email: ["e-?post", "e-?mail", "mejl"]`, `phone: ["telefon", "mobil", "phone"]`, `linkedin: ["linkedin"]`, `cv: ["\\bcv\\b", "resume", "résumé", "curriculum"]`, `letter: ["personligt brev", "cover.?letter", "motivation"]`
  - `classify_field(descriptor: str) -> str | None`
  - `@dataclass FillReport`: `filled: list[str]`, `unanswered: list[str]` (etiketter)
  - `class FormAdapter(Protocol)`: `name: str`; `matches(self, url: str, html: str) -> bool`; `fill(self, page: playwright.sync_api.Page, applicant: Applicant) -> FillReport`
  - `GenericAdapter` (`name = "generic"`, `matches` alltid sant)
  - `highlight(page: Page, selectors: list[str]) -> None` (sätter `outline: 3px solid #e11d48` via injicerad CSS)
  - `FormApplier(config: Callable[[], Config], paths: Paths, adapters: list[FormAdapter], headless: bool = False)`; `submit(job, draft) -> SubmitResult(channel=form, status=submitting, notes=report.unanswered)`; `close() -> None`

Regler:
- Textfält/textarea som klassas `letter` får `letter_text`; filfält som klassas `letter` får `letter_pdf`; filfält `cv` får `cv_path`.
- Synliga fält som är tomma efter ifyllning och är `required` eller saknar klassning läggs i `unanswered` och markeras. Kryssrutor för samtycke/villkor (`samtycke|villkor|consent|terms|gdpr|integritet`) rörs aldrig och listas alltid i `unanswered`.
- Inga klick på `button[type=submit]`, `input[type=submit]` eller knappar med texten skicka/submit/send/ansök/apply. Inga `form.submit()`/Enter i fält.
- Playwright sync-API körs i **en** dedikerad bakgrundstråd som ägs av `FormApplier` (`persistent_context` i `paths.browser`, Chromium). `submit` lägger uppdrag i en kö och väntar på `FillReport` (timeout 120 s). Sidan lämnas öppen för användaren. Undantag i tråden (eller timeout) → `SubmitError`.
- Adapter väljs i listordning; `GenericAdapter` ska ligga sist.

- [ ] **Step 1: Skriv fixture** `unknown.html`: förnamn, efternamn, e-post, telefon, LinkedIn, CV-uppladdning, textarea "Personligt brev", ett obligatoriskt fält "Löneanspråk", en samtyckesruta, en skicka-knapp, och `<script>` som sätter `window.__submitted = true` i både knappens `onclick` och formulärets `onsubmit`.

- [ ] **Step 2: Skriv failing tests** (pytest-playwright `page`, `page.goto(fixture.as_uri())`):
  - `test_classify_field` parametriserad: `"Förnamn *"` → `first_name`, `"E-postadress"` → `email`, `"Bifoga CV"` → `cv`, `"Cover letter"` → `letter`, `"Löneanspråk"` → `None`.
  - `test_generic_fills_known_fields`: fältvärden stämmer; filfältet har `cv.pdf` (`input.files[0].name`); textarea innehåller brevtexten.
  - `test_unanswered_reported_and_highlighted`: `unanswered` innehåller `"Löneanspråk"` och samtyckesrutans etikett; elementen har den injicerade outline-stilen.
  - `test_never_submits`: `page.evaluate("window.__submitted === true")` är falskt och URL:en oförändrad efter `fill`.
  - `test_consent_untouched`: samtyckesrutan är fortfarande ikryssad = falskt.
  - `test_applier.py::test_submit_returns_submitting_with_notes`: `FormApplier(headless=True)` mot fixturens `file://`-URL → `status == submitting`, `notes` innehåller `"Löneanspråk"`; anropa `close()`.
  - `test_applier_error_becomes_submit_error`: ogiltig URL → `SubmitError`.

- [ ] **Step 3: Kör** `uv run pytest tests/apply/form -v`. Förväntat: FAIL.

- [ ] **Step 4: Implementera.**

- [ ] **Step 5: Kör** testet. Förväntat: PASS.

- [ ] **Step 6: Commit** `git commit -m "feat: generic form filling in a supervised browser"`

---

### Task 14: Adaptrar för Teamtailor, Varbi och ReachMee

**Files:**
- Create: `src/jobaplier/apply/form/teamtailor.py`, `varbi.py`, `reachmee.py`
- Modify: `src/jobaplier/apply/form/__init__.py` (`default_adapters() -> list[FormAdapter]` = `[TeamtailorAdapter(), VarbiAdapter(), ReachMeeAdapter(), GenericAdapter()]`)
- Test: `tests/apply/form/test_ats_adapters.py`, `tests/fixtures/forms/teamtailor.html`, `varbi.html`, `reachmee.html`

**Interfaces:**
- Consumes: `FormAdapter`, `FillReport`, `Applicant`, `GenericAdapter`, `highlight` (Task 13); `detect_ats` (Task 5)
- Produces: `TeamtailorAdapter`, `VarbiAdapter`, `ReachMeeAdapter` (`name` = `"teamtailor"`, `"varbi"`, `"reachmee"`); `default_adapters()`

Varje adapter: `matches` = `detect_ats(url) == name` eller systemets HTML-signatur (generatorns meta-tagg / karaktäristisk form-id). `fill` fyller först systemets kända fält med fasta selektorer, kör sedan `GenericAdapter` på det som återstår och slår ihop rapporterna. Samma förbud som i Task 13.

- [ ] **Step 1: Skriv fixtures.** För varje system: spara ansökningsformuläret från en riktig annons ("Spara sida som… → bara HTML"), ta bort `<script>` och spårning, behåll formulärets markup, och lägg till samma `window.__submitted`-skript som i Task 13. Om formuläret laddas med JavaScript: kopiera den renderade DOM:en från webbläsarens utvecklarverktyg.

- [ ] **Step 2: Skriv failing tests** per adapter: `test_<ats>_matches`, `test_<ats>_fills_core_fields` (namn, e-post, telefon, CV, brev), `test_<ats>_never_submits`, samt `test_default_adapters_order` (generisk sist) och `test_generic_used_for_unknown` (fixturen från Task 13 väljer `GenericAdapter`).

- [ ] **Step 3: Kör** `uv run pytest tests/apply/form/test_ats_adapters.py -v`. Förväntat: FAIL.

- [ ] **Step 4: Implementera.**

- [ ] **Step 5: Kör** `uv run pytest tests/apply/form -v`. Förväntat: PASS.

- [ ] **Step 6: Commit** `git commit -m "feat: Teamtailor, Varbi and ReachMee form adapters"`

---

### Task 15: ApplyService – flödet för länk, utkast och ansökan

**Files:**
- Create: `src/jobaplier/apply/service.py`
- Test: `tests/apply/test_service.py`

**Interfaces:**
- Consumes: `transition`, modeller (Task 2); `AIClient`, `AIError` (Task 3); `load_or_build_profile` (Task 4); `write_letter`, `load_examples`, `word_count` (Task 11); `Applier`, `SubmitError`, `SubmitResult` (Task 12)
- Produces:
  - `class AlreadyHandled(Exception)`, `class NoAutoApply(Exception)`
  - `ApplyService(engine: Engine, ai: AIClient, paths: Paths, config: Callable[[], Config], appliers: dict[ApplyMethod, Applier])`
  - `mark_link(job_id: int) -> Job` (`scored → link`)
  - `dismiss(job_id: int) -> Job` (från `scored` eller `draft_ready`)
  - `start_draft(job_id: int) -> Draft` (`scored → drafting`, skriv brev, `drafting → draft_ready`; `AIError` → `drafting → scored` och kastar vidare)
  - `regenerate(job_id: int, instruction: str) -> Draft` (kräver `draft_ready`, ny version med `previous` = senaste texten)
  - `save_edit(job_id: int, letter_text: str) -> Draft` (ny version, `instruction = "manuell redigering"`, `unsupported_claims_json = []`)
  - `latest_draft(job_id: int) -> Draft | None`
  - `approve_and_submit(job_id: int, draft_id: int) -> Application`
  - `mark_submitted(job_id: int) -> Application` (från `submitting` [formulär] → `submitted`; från `link` → `submitted` med `Application(channel=manual)`)
  - `retry(job_id: int) -> Job` (`failed → draft_ready`)
  - `emails_sent_today(engine: Engine, now: datetime | None = None) -> int` (applications med `channel=email`, `status=submitted`, `dry_run=False`, `submitted_at` sedan lokal midnatt; används som `sent_today` i Task 12)

`approve_and_submit`:
1. Under en `threading.Lock` i tjänsten och i en egen transaktion: läs jobbet; om status inte är `draft_ready` → `AlreadyHandled`. `apply_method == unknown` → `NoAutoApply`. `draft_ready → approved → submitting`; skapa eller uppdatera `Application(job_id, channel, status=submitting, draft_id)`; **commit innan appliern anropas**.
2. Anropa `appliers[job.apply_method].submit(job, draft)` utanför låset.
3. Resultat `submitted` → jobbet `submitted`, `Application.status/receipt/dry_run/submitted_at`. Resultat `submitting` (formulär) → jobbet stannar `submitting`, `notes` sparas i `Application.error` som `"Obesvarade frågor: …"`. `SubmitError` → jobbet `failed`, `Application.status=failed`, `error=str(e)`. Övriga undantag i formulärvägen → `submitting → draft_ready`, `Application.status=failed`.

- [ ] **Step 1: Skriv failing tests** (`FakeAI`, `FakeGmail`, falsk formulär-applier):
  - `test_link_and_dismiss`.
  - `test_start_draft_creates_version_1` (+ `language` sparas), `test_start_draft_ai_error_returns_to_scored`.
  - `test_regenerate_and_edit_increment_version`.
  - `test_email_happy_path`: jobbet `submitted`, `Application.receipt == "fake-1"`.
  - `test_concurrent_approve_sends_once` (Review Focus 2): `FakeGmail(send_delay=0.2)`, två trådar anropar `approve_and_submit` samtidigt → `len(FakeGmail.sent) == 1`, den ena tråden får `AlreadyHandled`.
  - `test_submit_error_marks_failed_then_retry`.
  - `test_form_path_stays_submitting_then_mark_submitted`.
  - `test_form_crash_returns_to_draft_ready`.
  - `test_unknown_method_raises_no_auto_apply`.
  - `test_link_mark_submitted_creates_manual_application`.
  - `test_emails_sent_today_excludes_dry_run`.

- [ ] **Step 2: Kör** `uv run pytest tests/apply/test_service.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: apply service orchestrating drafts and submissions"`

---

### Task 16: Webb – jobblista, detalj, åtgärder och "Sök nu"

**Files:**
- Create: `src/jobaplier/web/__init__.py`, `web/state.py`, `web/app.py`, `web/routes_jobs.py`, `web/templates/base.html`, `jobs.html`, `_job_row.html`, `job_detail.html`, `web/static/style.css`
- Test: `tests/web/conftest.py` (fixture `client` som bygger `AppState` med fakes), `tests/web/test_jobs.py`

**Interfaces:**
- Consumes: `ApplyService` (Task 15); `run_pipeline`, `RunInProgress` (Task 10); `monthly_spend` (Task 3); `load_config` (Task 1)
- Produces:
  - `@dataclass AppState`: `engine`, `paths: Paths`, `config: Callable[[], Config]`, `service: ApplyService`, `run_now: Callable[[], None]` (startar en körning i bakgrundstråd; kastar `RunInProgress` om en pågår), `setup_missing: Callable[[], list[str]]`, `on_config_saved: Callable[[Config], None] = lambda c: None` (Task 18 använder den för att lägga om schemat)
  - `create_app(state: AppState) -> FastAPI` (monterar `/static`, Jinja2-mallar, inkluderar routers; `state` nås via `request.app.state.jobaplier`)

Routes:
- `GET /` – jobb med status `scored` och `score >= threshold`, sorterade fallande på poäng. `?more=1` visar även de under tröskeln. `?status=<JobStatus>` och `?source=<namn>` filtrerar. Per rad: titel (länk till detalj), företag, ort, poäng, källor, motivering, ansökningsväg och knapparna *Ansök* (bara om `apply_method` är `email`/`form`), *Länk* och *Inte intressant*. Varningsbanner om senaste körningen har `errors_json`. AI-banner om senaste körningen har `errors_json["budget"]` (visar meddelandet), eller om `ai.backend == "api"` och `monthly_spend >= ai_monthly_budget_usd`. Knappen *Sök nu*.
- `GET /jobs/{id}` – hela annonsen, poäng, motivering, källor (med länkar), ansökningsväg, `filter_reason` om `filtered`. Ofullständiga jobb märks "ofullständig".
- `POST /jobs/{id}/link`, `POST /jobs/{id}/dismiss` – HTMX: returnerar uppdaterad `_job_row.html`.
- `POST /jobs/{id}/apply` – `service.start_draft`; svarar med `HX-Redirect: /jobs/{id}/draft`. `AIError` → raden visar "Kunde inte skriva brevet: …".
- `POST /run` – `state.run_now()`; flash "Sökning startad" eller "En sökning pågår redan".
- HTMX laddas från `https://unpkg.com/htmx.org@2.0.4`.

- [ ] **Step 1: Skriv failing tests:** `test_list_shows_above_threshold_only`, `test_more_shows_below_threshold`, `test_status_and_source_filters`, `test_no_apply_button_for_unknown` (Review Focus 4), `test_link_action_updates_row`, `test_dismiss_action`, `test_apply_redirects_to_draft`, `test_run_now_in_progress_message`, `test_source_error_banner`, `test_budget_banner_api`, `test_usage_limit_banner_claude_code`, `test_detail_shows_filter_reason`.

- [ ] **Step 2: Kör** `uv run pytest tests/web/test_jobs.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** testet. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: dashboard job list, detail and actions"`

---

### Task 17: Webb – utkast, ansökningar, körningar, inställningar och installationssida

**Files:**
- Create: `src/jobaplier/web/routes_drafts.py`, `web/routes_admin.py`, mallarna `draft.html`, `submitted.html`, `applications.html`, `runs.html`, `settings.html`, `setup.html`
- Modify: `src/jobaplier/web/app.py` (routers + setup-middleware)
- Test: `tests/web/test_drafts.py`, `tests/web/test_admin.py`

**Interfaces:**
- Consumes: `AppState`, `create_app` (Task 16); `ApplyService` med `AlreadyHandled`, `NoAutoApply` (Task 15); `save_config`, `Config` (Task 1); `word_count` (Task 11); `monthly_spend` (Task 3)
- Produces: routes nedan

Routes:
- `GET /jobs/{id}/draft` – annonsen och senaste utkastet sida vid sida; redigerbar textarea; ordantal; varningslista "Påståenden utan stöd i CV:t" från `unsupported_claims_json`; formulär för *Ny version* (fält `instruction`); knappen *Godkänn och skicka* (mejl) eller *Öppna formulär* (formulär); banner "Torrkörning är på – mejlet skickas till dig själv" när `dry_run`.
- `POST /jobs/{id}/draft` (`letter_text`) → `save_edit`. `POST /jobs/{id}/draft/regenerate` (`instruction`) → `regenerate`.
- `POST /jobs/{id}/draft/approve` (`draft_id`) → `approve_and_submit`. Mejl: `submitted.html` med mottagare och torrkörningsflagga. Formulär: "Formuläret är öppnat i webbläsaren. Obesvarade frågor: … Klicka Skicka där och sedan här." + knapp `POST /jobs/{id}/mark-submitted`. `AlreadyHandled` → "Ansökan är redan hanterad". `failed` → felmeddelande + knapp `POST /jobs/{id}/retry`.
- `POST /jobs/{id}/mark-submitted`, `POST /jobs/{id}/retry`.
- `GET /applications` – tabell: datum, jobb, kanal, status, torrkörning, kvitto/fel.
- `GET /runs` – senaste 50 körningarna med statistik och fel. Med `api`: AI-kostnad denna månad mot budget. Med `claude_code`: antal AI-anrop och tokens denna månad (från `ai_calls`), med texten "Körs via din Claude-prenumeration".
- `GET /settings` / `POST /settings` – formulär för alla fält i `Config`: listor som kommaseparerade fält, karriärsidor som textarea med en rad per sajt `typ,företag,id`. Ogiltiga värden → sidan visas igen med valideringsfelen; giltiga → `save_config`, `state.on_config_saved(config)` och "Sparat".
- Setup-middleware: om `state.setup_missing()` inte är tom omdirigeras alla sökvägar utom `/setup`, `/settings` och `/static/*` till `/setup`, som listar vad som saknas: `data/cv.pdf`, `data/config.yaml`, AI-backenden (med `claude_code`: "Claude Code hittas inte – installera och logga in med `claude`" om `claude_code_available(claude_bin)` är falskt; med `api`: `ANTHROPIC_API_KEY`), Gmail-inloggning (`jobaplier gmail-login`).

- [ ] **Step 1: Skriv failing tests:** `test_draft_page_shows_letter_warnings_and_wordcount`, `test_dry_run_banner`, `test_save_edit_creates_new_version`, `test_regenerate_passes_instruction`, `test_approve_email_shows_submitted`, `test_approve_twice_shows_already_handled`, `test_form_approve_shows_unanswered_and_mark_button`, `test_failed_shows_retry`, `test_applications_list`, `test_runs_page_shows_spend_api`, `test_runs_page_shows_subscription_usage`, `test_settings_roundtrip_writes_yaml` (och `on_config_saved` anropas en gång), `test_settings_validation_error_shown` (`schedule` = `"25:00"`), `test_setup_redirect_when_missing`, `test_setup_lists_missing_items`, `test_setup_api_key_only_required_for_api_backend`.

- [ ] **Step 2: Kör** `uv run pytest tests/web -v`. Förväntat: FAIL för de nya testerna.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör** `uv run pytest tests/web -v`. Förväntat: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: draft review, history, settings and setup pages"`

---

### Task 18: Schemaläggare, koppling, CLI, launchd och eval-skript

**Files:**
- Create: `src/jobaplier/scheduler.py`, `src/jobaplier/main.py`, `src/jobaplier/cli.py`, `launchd/se.jobaplier.plist.template`, `scripts/eval_prompts.py`
- Modify: `README.md` (drift, första start, Gmail-inloggning, LinkedIn-aviseringar, launchd)
- Test: `tests/test_scheduler.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: allt ovan
- Produces:
  - `build_scheduler(run_job: Callable[[], None], times: list[str], tz: str = "Europe/Stockholm") -> BackgroundScheduler` (en `CronTrigger(hour, minute)` per tid; `coalesce=True`, `misfire_grace_time=6 * 3600`, `max_instances=1`; jobb-id `f"run-{hh}{mm}"`)
  - `build_sources(config: Config, paths: Paths, http: httpx.Client) -> list[Source]` (aktiverade källor; LinkedIn hoppas över med en loggvarning om Gmail-token saknas, och får då ett fel i körningen som syns i bannern)
  - `build_state(paths: Paths) -> AppState` (AI via `make_ai(config, engine, paths)`; för `api` skapas klienten som `anthropic.Anthropic(max_retries=4)`; `GoogleGmail`, `EmailApplier`, `FormApplier(default_adapters())`, `ApplyService`, `run_now` i bakgrundstråd, `setup_missing`)
  - `create_production_app() -> FastAPI` (lifespan startar schemaläggaren och stänger den samt `FormApplier.close()` vid avslut; schemat läses om när inställningarna sparas)
  - CLI `jobaplier` (argparse): `serve` (uvicorn `127.0.0.1:8000`), `gmail-login` (`run_oauth_flow`), `run-once` (en körning, skriver ut statistik), `install-launchd` (skriver `~/Library/LaunchAgents/se.jobaplier.plist` från mallen med absoluta sökvägar till `uv` och projektet, och skriver ut `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/se.jobaplier.plist`)
  - Plist-mallen: `Label se.jobaplier`, `ProgramArguments [<uv>, run, --project, <dir>, jobaplier, serve]`, `WorkingDirectory <dir>`, `RunAtLoad true`, `KeepAlive true`, `StandardOutPath <dir>/data/logs/jobaplier.log`, `StandardErrorPath <dir>/data/logs/jobaplier.err.log`, `EnvironmentVariables` med `PATH` = katalogen för `shutil.which(config.ai.claude_bin)` + `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` och `HOME` = användarens hemkatalog (Claude Codes inloggning ligger där och i nyckelringen)
  - `scripts/eval_prompts.py`: läser `data/eval/*.json` (`{"title", "company", "description"}`), kör `score_job` och `write_letter` med AI från `make_ai` (den konfigurerade backenden) och skriver ut poäng, motivering, brev, ordantal och påståenden utan stöd. Ingår inte i testsviten.

- [ ] **Step 1: Skriv failing tests:**
  - `test_scheduler_jobs_and_options`: `build_scheduler(fn, ["08:00", "12:00", "17:00"])` → tre jobb med timmarna 8, 12, 17; varje jobb har `coalesce is True`, `misfire_grace_time == 21600`, `max_instances == 1`.
  - `test_cli_parses_commands`: `serve`, `gmail-login`, `run-once`, `install-launchd` ger rätt underkommando.
  - `test_plist_render`: renderad plist innehåller projektets absoluta sökväg, `se.jobaplier` och `<key>KeepAlive</key>`, är giltig enligt `plistlib.loads`, och `EnvironmentVariables.PATH` innehåller katalogen där `claude` hittades.
  - `test_build_sources_respects_toggles`: `platsbanken.enabled=False` → ingen `PlatsbankenSource`; två karriärsajter → två karriärkällor.

- [ ] **Step 2: Kör** `uv run pytest tests/test_scheduler.py tests/test_cli.py -v`. Förväntat: FAIL.

- [ ] **Step 3: Implementera.**

- [ ] **Step 4: Kör hela sviten** `uv run pytest -v`. Förväntat: alla PASS.

- [ ] **Step 5: Manuell röktest** (Claude Code inloggat, `ai.backend: claude_code`, `dry_run: true`): `uv run jobaplier gmail-login`, sedan `uv run jobaplier run-once`. Förväntat: statistik utan fel för Platsbanken. Starta `uv run jobaplier serve`, öppna `http://127.0.0.1:8000`, välj ett mejljobb → Ansök → Godkänn och skicka. Förväntat: mejlet kommer till din egen inkorg med ämnet `[TORRKÖRNING → …]` och två PDF-bilagor. Välj ett Teamtailor-jobb → Öppna formulär. Förväntat: Chromium öppnas med fälten ifyllda, obesvarade frågor markerade och inget skickat.

- [ ] **Step 6: Commit** `git commit -m "feat: scheduler, CLI, launchd agent and prompt eval script"`
