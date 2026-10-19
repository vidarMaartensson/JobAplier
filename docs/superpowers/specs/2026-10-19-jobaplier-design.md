# JobAplier – design

Datum: 2026-10-19
Status: Godkänd i dialog, väntar på granskning av spec

## 1. Syfte och mål

En personlig tjänst som körs lokalt på en Mac, regelbundet letar jobb som passar
användarens CV och preferenser, och presenterar dem i en lista i en webbdashboard.
För varje jobb väljer användaren:

- **Länk** – jobbet sparas och användaren söker själv, eller
- **Ansök** – tjänsten skriver ett anpassat personligt brev och förbereder
  ansökan; användaren granskar och godkänner med ett klick innan något skickas.

**Användare:** en person (ägaren). Inte en produkt för andra.

**Lyckat resultat:**

- Användaren behöver inte leta jobb själv; listan är kort och relevant.
- Varje jobb i listan har en poäng och en begriplig motivering.
- Godkända mejlansökningar skickas utan manuellt arbete; formulär fylls i så långt
  det går och användaren behöver bara granska och klicka på Skicka.
- Ingenting skickas i användarens namn utan att användaren har sett det.

## 2. Avgränsningar

**Ingår i v1**

- Källor: Platsbanken (JobTech JobSearch API), LinkedIn via jobbaviseringsmejl,
  företagens karriärsidor (Greenhouse, Lever, Teamtailor).
- Matchning: hårt filter på preferenser + AI-poäng mot CV.
- Ansökningssätt: mejl (skickas automatiskt efter godkännande) och formulär
  (fylls i i synlig webbläsare, användaren klickar Skicka). Övriga jobb: länk.
- Lokal drift på macOS, startad av launchd.

**Ingår inte i v1**

- LinkedIn Easy Apply eller annan automatisering av LinkedIn-kontot.
- Scraping av LinkedIn med inloggning.
- Server-/molndrift, inloggning till dashboarden.
- Att tjänsten lär sig av användarens val.
- Uppföljning/påminnelser efter ansökan.
- Flera CV:n eller profiler.
- Att lösa CAPTCHA eller klicka på Skicka i formulär.

## 3. Arkitektur

En Python-process (`jobaplier`) som innehåller:

- en **FastAPI**-webbapp på `http://localhost:8000` (Jinja-mallar + HTMX),
- en **APScheduler**-schemaläggare i samma process som kör sökningen enligt ett
  schema (standard 08:00, 12:00, 17:00) och på begäran via knappen "Sök nu".

Processen startas av en launchd-agent vid inloggning och startas om om den
kraschar (`KeepAlive`). All data lagras i SQLite (`data/jobaplier.db`).

### 3.1 Teknik

| Del | Val |
|---|---|
| Språk / paket | Python 3.12+, `uv` för beroenden |
| Webb | FastAPI, Jinja2, HTMX |
| Databas | SQLite via SQLModel |
| Schemaläggning | APScheduler (`coalesce=True`, `misfire_grace_time` så missade körningar körs när datorn vaknar) |
| HTTP | httpx |
| HTML-tolkning | selectolax eller BeautifulSoup |
| PDF in | pypdf (läsa CV) |
| PDF ut | WeasyPrint (personligt brev → PDF) |
| Webbläsare | Playwright (Chromium, synligt läge, persistent profil) |
| Mejl | Gmail API (google-api-python-client), OAuth |
| AI | Anthropic Python SDK. `claude-haiku-5-5` för poängsättning, `claude-sonnet-5-5` för personliga brev och CV-profil |
| Validering | Pydantic |
| Test | pytest, pytest-playwright |

### 3.2 Projektstruktur

```
JobAplier/
  pyproject.toml
  src/jobaplier/
    main.py            # startar FastAPI + scheduler
    config.py          # läser data/config.yaml och .env
    db.py              # engine, sessioner, migrering (create_all)
    models.py          # SQLModel-tabeller
    ai.py              # tunn wrapper kring Anthropic-klienten (utbytbar i test)
    scheduler.py
    profile/           # CV-inläsning och profil
    sources/           # en modul per källa
      base.py          # Source-protokoll, RawJob
      platsbanken.py
      linkedin_alerts.py
      careers/         # greenhouse.py, lever.py, teamtailor.py
    pipeline/          # dedupe.py, filter.py, scoring.py, run.py
    apply/
      letter.py        # skriver personligt brev
      email.py         # EmailApplier
      form/            # FormApplier + adaptrar (generic, teamtailor, varbi, reachmee)
    web/               # routes, templates, static
  tests/
    fixtures/          # sparade API-svar, mejl, HTML-formulär
  scripts/eval_prompts.py
  launchd/se.jobaplier.plist
  data/                # gitignorerad: db, CV, token, browserprofil, config.yaml
```

## 4. Komponenter

Varje komponent har ett ansvar och ett litet gränssnitt, och kan testas isolerat.

### 4.1 `profile`

- Användaren lägger sitt CV som `data/cv.pdf`.
- Vid första start, eller när filen ändras (hash jämförs), läses texten med
  pypdf och Sonnet extraherar en strukturerad `Profile`: kompetenser,
  erfarenhetsår, tidigare roller och titlar, språk, utbildning.
- Profilen sparas i databasen tillsammans med hela CV-texten.
- Preferenser läses från `data/config.yaml` (se avsnitt 7). Inställningssidan i
  dashboarden visar och redigerar samma fil.
- Gränssnitt: `get_profile() -> Profile`, `get_preferences() -> Preferences`.

### 4.2 `sources`

Gemensamt protokoll:

```python
class Source(Protocol):
    name: str
    def fetch(self, since: datetime) -> list[RawJob]: ...
```

`RawJob` har: `source`, `external_id`, `title`, `company`, `location`,
`remote` (bool | None), `description`, `url`, `published_at`,
`apply` (`ApplyInfo`: `method` = `email` | `form` | `unknown`, `email`, `form_url`),
`incomplete` (bool).

**Platsbanken** (`platsbanken.py`): anropar JobTech JobSearch API (`/search`) med
nyckelord och orter/regioner från preferenserna och `published-after=since`.
Sidindelning hanteras. `application_details` ger mejladress eller URL; en URL som
pekar på ett känt rekryteringssystem ger `method=form`.

**LinkedIn-aviseringar** (`linkedin_alerts.py`): användaren har LinkedIns
jobbaviseringar påslagna. Adaptern läser mejl från LinkedIns aviseringsavsändare
nyare än `since` via Gmail-API:t, plockar ut jobblänkar, titel och företag, och
hämtar sedan den publika jobbsidan (utan inloggning) för beskrivningen. Går det
inte att hämta sidan sparas jobbet med `incomplete=True`. LinkedIn-jobb får alltid
`method=unknown` (visas som länk) om inte beskrivningen pekar på en extern
ansökningsväg.

**Karriärsidor** (`careers/`): en lista över företag i `config.yaml`, var och en
med typ (`greenhouse`, `lever`, `teamtailor`) och identifierare. Greenhouse och
Lever använder sina publika JSON-API:er; Teamtailor läser karriärsidans publika
jobbflöde. Ansökningsväg blir `form` med jobbets ansökningsadress.

Varje källa körs isolerat med timeout (30 s per anrop) och upp till 3 nya försök
med exponentiell backoff vid nätverksfel.

### 4.3 `pipeline`

`run_pipeline()` gör en sökkörning:

1. Hämtar från alla aktiverade källor med `since` = förra lyckade körningen för
   den källan (första gången: 14 dagar bakåt).
2. **Dedupe:** först `(source, external_id)`; sedan normaliserad nyckel
   `company + title` (gemener, utan specialtecken, utan ord som "AB"). En träff
   kopplar den nya förekomsten till befintligt jobb. Bästa ansökningsväg behålls
   i ordningen `email` > `form` > `unknown`.
3. **Hårt filter** (ingen AI): uteslutna företag, uteslutna nyckelord i titel,
   orter/distans, senioritet. Bortfiltrerade jobb får status `filtered` och ett
   `filter_reason`.
4. **AI-poäng** med Haiku för jobb som klarat filtret. Indata: profil,
   preferenser, annons. Utdata valideras mot ett Pydantic-schema:
   `score` (0–100), `reasons` (2–3 meningar), `apply_method_hint`.
   Ofullständiga jobb poängsätts på titel och företag.
5. Jobben får status `scored`. Dashboarden visar jobb med `score >= threshold`
   (standard 60). Övriga visas under "Visa fler".

Körningen loggas i `runs`: start, slut, antal nya, antal filtrerade, antal
poängsatta, fel per källa, AI-kostnad.

### 4.4 `apply`

**Personligt brev** (`letter.py`): Sonnet får annonsen, profilen, hela CV-texten
och valfritt 1–2 stilexempel (`data/letter_examples/*.txt`). Regler i prompten:

- samma språk som annonsen (svenska eller engelska),
- bara fakta som finns i CV:t; påståenden utan stöd i CV:t ska markeras,
- ungefär 250–350 ord.

Utdata: brevtext och en lista med påståenden som modellen inte kunde knyta till
CV:t. Listan visas som varningar i granskningsvyn.

Användaren kan redigera fritt eller be om en ny version med en instruktion. Varje
version sparas i `drafts`.

**Gemensamt gränssnitt:**

```python
class Applier(Protocol):
    def submit(self, job: Job, draft: Draft) -> SubmitResult: ...
```

**`EmailApplier`:**

- Gmail API med OAuth. Behörigheter: `gmail.send` och `gmail.readonly`.
  `credentials.json` och token ligger i `data/`.
- Mottagare: mejladressen från annonsen. Ämne: annonsens referens om den finns,
  annars `Ansökan: <titel>`. Brödtext: en kort hälsning.
- Bilagor: `data/cv.pdf` som `CV.pdf` och brevet renderat till
  `Personligt_brev.pdf`.
- **Torrkörningsläge** (`dry_run: true`, standard): mejlet skickas till
  användarens egen adress med originalmottagaren angiven i ämnesraden.
- Gmails `message_id` sparas i `applications`.

**`FormApplier`:**

- Playwright Chromium i synligt läge med persistent profil i `data/browser/`.
- Öppnar formulärets URL, väljer adapter efter domän eller HTML-signatur
  (Teamtailor, Varbi, ReachMee), annars den generella adaptern.
- Generell adapter: matchar fält på etikett, `name`, `placeholder` och
  `aria-label` mot kända fält (förnamn, efternamn, mejl, telefon, LinkedIn,
  CV-uppladdning, brev som text eller fil).
- Frågor som inte kan besvaras säkert lämnas tomma, markeras visuellt i sidan
  (injicerad CSS-ram) och listas i dashboarden.
- Klickar **aldrig** på Skicka och löser aldrig CAPTCHA. Jobbet får status
  `submitting`. Användaren klickar Skicka i webbläsaren och sedan "Markera som
  skickad" i dashboarden, varpå status blir `submitted`.

**`LinkOnly`:** ingen åtgärd. Jobbet får status `link`, och användaren kan senare
markera det som "ansökt själv" (`submitted`, kanal `manual`).

**Säkerhetsspärrar:**

- Unikt villkor i `applications` på `(job_id, channel)`; ett nytt försök efter
  `failed` uppdaterar samma rad.
- Dagstak för mejlansökningar (`max_emails_per_day`, standard 10).
- Inget skickas automatiskt om; `failed` kräver ett manuellt nytt försök.
- Torrkörning på tills användaren stänger av den i `config.yaml`.

### 4.5 `web`

Sidor:

- **Jobb** (startsida): lista sorterad efter poäng, filter per status och källa,
  knapparna *Ansök*, *Länk* och *Inte intressant* per rad. Varningsbanner om en
  källa misslyckats i senaste körningen. Knappen *Sök nu*.
- **Jobbdetalj:** hela annonsen, poäng, motivering, källor, ansökningsväg.
- **Utkast:** annons och brev sida vid sida, redigerbar text, varningar om
  påståenden utan stöd i CV:t, *Ny version (med instruktion)*, *Godkänn och skicka*
  respektive *Öppna formulär*.
- **Ansökningar:** historik över skickade ansökningar.
- **Inställningar:** preferenser, källor, företagslista, tröskel, schema,
  torrkörning, dagstak, AI-kostnadstak. Skriver till `config.yaml`.
- **Körningar:** logg från `runs` och månadskostnad för AI.

Dashboarden lyssnar bara på `127.0.0.1`.

## 5. Datamodell

**`jobs`:** `id`, `dedupe_key` (unik), `title`, `company`, `location`, `remote`,
`description`, `url`, `published_at`, `apply_method`, `apply_email`,
`apply_form_url`, `incomplete`, `status`, `filter_reason`, `score`,
`score_reasons`, `created_at`, `updated_at`.

**`job_sources`:** `id`, `job_id`, `source`, `external_id`, `url`, `raw_json`,
`fetched_at`. Unikt på `(source, external_id)`.

**`profiles`:** `id`, `cv_hash`, `cv_text`, `profile_json`, `created_at`.

**`drafts`:** `id`, `job_id`, `version`, `letter_text`, `unsupported_claims_json`,
`instruction`, `created_at`.

**`applications`:** `id`, `job_id`, `draft_id`, `channel` (`email` | `form` |
`manual`), `status` (`submitting` | `submitted` | `failed`), `receipt`
(t.ex. Gmail message-id), `error`, `dry_run`, `submitted_at`.
Unikt på `(job_id, channel)`.

**`runs`:** `id`, `started_at`, `finished_at`, `trigger` (`schedule` | `manual`),
`stats_json`, `errors_json`, `ai_cost_usd`.

### 5.1 Statusar för jobb

```
new → filtered
new → scored
scored → dismissed | link | drafting
drafting → draft_ready
drafting → scored  (brevet kunde inte skrivas; felet visas)
draft_ready → approved | dismissed
approved → submitting → submitted
submitting → failed → draft_ready | link
link → submitted   (användaren markerar "ansökt själv")
```

Övergångar sker bara via en funktion (`transition(job, to)`), som avvisar
otillåtna övergångar.

## 6. Felhantering

- **Källor:** isolerade; fel loggas per källa i `runs` och visas som varning.
  Övriga källor körs vidare. Utgången Gmail-token ger ett tydligt meddelande med
  länk för att logga in igen.
- **Ändrade sajter / tolkningsfel:** jobbet sparas med det som finns och
  `incomplete=True`, i stället för att försvinna.
- **Claude-API:** nya försök med backoff vid fel och rate limit. Ogiltigt svar
  ger ett nytt försök, och därefter manuell granskning. Jobbet ligger kvar som
  `new` om poängsättningen inte gick att genomföra, och tas med i nästa körning.
- **AI-kostnadstak** (`ai_monthly_budget_usd`): när taket nås pausas AI-anrop och
  dashboarden visar en banner.
- **Utskick:** Gmail-fel ger `failed` med felmeddelande. Krasch i Playwright ger
  `draft_ready` igen.
- **Missade körningar:** APScheduler kör en missad körning när datorn vaknar
  (`coalesce=True`).
- **Samtidighet:** bara en sökkörning åt gången (lås); "Sök nu" medan en körning
  pågår ignoreras med ett meddelande.

## 7. Konfiguration

`.env` (gitignorerad): `ANTHROPIC_API_KEY`.

`data/config.yaml` (gitignorerad, exempel i `config.example.yaml`):

```yaml
profile:
  name: ...
  email: ...
  phone: ...
  linkedin_url: ...
preferences:
  roles: ["backend developer", "fullstack"]
  keywords: ["python", "typescript"]
  exclude_keywords: ["senior manager"]
  exclude_companies: []
  locations: ["Stockholm"]
  remote_ok: true
  seniority: ["junior", "mid"]
sources:
  platsbanken: { enabled: true }
  linkedin_alerts: { enabled: true }
  careers:
    - { type: teamtailor, company: "Exempel AB", id: "exempel" }
scoring:
  threshold: 60
schedule: ["08:00", "12:00", "17:00"]
apply:
  dry_run: true
  max_emails_per_day: 10
ai_monthly_budget_usd: 10
```

## 8. Testning

Testdriven utveckling med pytest. Inga nätverksanrop i testsviten.

- **Källor:** sparade svar som fixtures (JobTech-JSON, LinkedIn-aviseringsmejl,
  publik LinkedIn-jobbsida, Greenhouse-, Lever- och Teamtailor-svar). Testerna
  kontrollerar normalisering till `RawJob` och ansökningsväg.
- **Pipeline:** dedupe, hårt filter, statusövergångar och att fel i en källa inte
  stoppar andra. AI ersätts med en falsk klient med förutsägbara svar.
- **Brev:** att prompten innehåller CV och annons, att språkval och varningar om
  påståenden utan stöd tas om hand (falsk AI-klient).
- **EmailApplier:** falsk Gmail-klient. Mottagare, ämne, bilagor, torrkörning,
  dagstak, skydd mot dubbelutskick.
- **FormApplier:** pytest-playwright mot lokala HTML-sidor som efterliknar
  Teamtailor-, Varbi- och ReachMee-formulär samt ett okänt formulär. Testerna
  kontrollerar ifyllda fält, markerade obesvarade frågor och att Skicka **aldrig**
  klickas.
- **Webb:** FastAPI TestClient för flödena lista → ansök → utkast → godkänn och
  lista → länk.
- **Prompter:** `scripts/eval_prompts.py` kör poängsättning och brevskrivning mot
  5–10 sparade riktiga annonser och skriver ut resultatet för manuell granskning.
  Ingår inte i testsviten.

## 9. Drift

- `uv run jobaplier` startar tjänsten.
- `launchd/se.jobaplier.plist` installeras i `~/Library/LaunchAgents/` och startar
  tjänsten vid inloggning med `KeepAlive`. Loggar till `data/logs/`.
- Första start: tjänsten kontrollerar att `data/cv.pdf`, `data/config.yaml`,
  `ANTHROPIC_API_KEY` och Gmail-OAuth finns, och visar annars en
  installationssida med det som saknas.
