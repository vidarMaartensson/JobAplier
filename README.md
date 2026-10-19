# JobAplier

Lokal Python-tjänst (FastAPI + SQLite) som hittar jobb som matchar ditt CV.

## Förutsättningar

- macOS med Homebrew och [uv](https://docs.astral.sh/uv/)
- Systembibliotek för WeasyPrint: `brew install pango`
- Claude Code installerat och inloggat med din prenumeration (standardbackend `claude_code`).
  Med `ai.backend: api` behövs i stället `ANTHROPIC_API_KEY` i `.env` (se `.env.example`).

## Installation

```sh
uv sync
uv run playwright install chromium
```

## Konfiguration

1. Lägg ditt CV i `data/cv.pdf`.
2. Kopiera exempelkonfigurationen och redigera den:

   ```sh
   mkdir -p data
   cp config.example.yaml data/config.yaml
   ```

`data/` och `.env` är gitignorerade.

## Första start

```sh
uv run jobaplier serve        # eller bara `uv run jobaplier`
```

Öppna <http://127.0.0.1:8000>. Dashboarden lyssnar bara på `127.0.0.1:8000`. Så länge
något saknas (CV, inställningar, Claude Code eller API-nyckel) visas
installationssidan `/setup` med en lista över vad som återstår. Inställningarna kan fyllas
i under **Inställningar** i dashboarden; de sparas i `data/config.yaml` och schemat läggs om
direkt. Ändrar du `data/config.yaml` för hand medan tjänsten kör, starta om den.

## Drift

| Kommando | Vad det gör |
|---|---|
| `uv run jobaplier serve` | Dashboard på `http://127.0.0.1:8000` och schemalagda sökningar (standard 08:00, 12:00, 17:00, Europe/Stockholm). |
| `uv run jobaplier run-once` | En sökkörning direkt, skriver ut statistik och fel. Inte samtidigt som `serve` (se nedan). |
| `uv run jobaplier install-launchd` | Skriver launchd-agenten (se nedan). |

Kör inte `jobaplier run-once` medan `jobaplier serve` (eller launchd-agenten) är igång.
Spärren för "en körning åt gången" gäller bara inom en process, så två processer kan söka
och poängsätta samtidigt mot samma databas. Använd *Sök nu* i dashboarden i stället, eller
stoppa tjänsten först.

Missade schemalagda körningar (datorn sov) görs när datorn vaknar, om det är högst sex
timmar sedan; flera missade körningar slås ihop till en. "Sök nu" i dashboarden startar en
körning direkt; bara en körning åt gången.

JobAplier skickar aldrig något själv: varken mejl eller formulär. Varje ansökan förbereds
som ett paket (se nedan) som du skickar själv.

Brev som höll på att skrivas när tjänsten stängdes blir poängsatta jobb igen, och en körning
som avbröts markeras som avbruten.

### Ansökningspaketet

*Ansök* på ett jobb skriver ett personligt brev och ett skräddarsytt CV. Utkastsidan
(`/jobs/<id>/draft`) visar dem under **Ansökningspaket**:

- **Ladda ner CV (PDF)** och **Ladda ner personligt brev (PDF)**. Filerna heter
  `CV_<Namn>_<Företag>.pdf` och `Personligt_brev_<Företag>.pdf` (å/ä/ö blir a/a/o).
  Brev-PDF:en skapas av texten i brevrutan just nu, även osparade ändringar.
- Brevet går att redigera. **Ny version** med en instruktion gör om både brev och CV.
- Vad du gör sedan beror på hur jobbet söks:
  - **Mejl**: mottagare, ämne och brödtext med var sin **Kopiera**-knapp, och
    **Öppna i mejlprogrammet** (en `mailto:`-länk, bara om adressen är giltig). Länken
    kan inte bifoga filer, så bifoga de två PDF:erna själv.
  - **Formulär**: **Fyll i automatiskt** öppnar formuläret i en synlig Chromium och fyller
    i det med det skräddarsydda CV:t och brevet, men skickar aldrig. Du granskar och klickar
    själv på Skicka, och trycker sedan **Jag har skickat** på jobbet. **Öppna formuläret**
    öppnar det i en ny flik om du hellre fyller i själv; tryck då **Jag har skickat**
    (bredvid knappen) när ansökan är skickad.
  - **Okänt** (t.ex. importerade LinkedIn-jobb): **Öppna annonsen** och följ dess
    instruktioner; ladda upp PDF:erna eller klistra in brevet med **Kopiera**.
- **Jag har skickat** (alla ansökningssätt) sparar brevrutans text som en version och flyttar
  jobbet till dina ansökningar. Är sidan gammal (det finns en nyare version av utkastet)
  markeras ingenting – ladda om sidan.

### Det skräddarsydda CV:t

Första gången ett utkast skapas tolkar Claude ditt `data/cv.pdf` till ett strukturerat CV.
Det sparas och byggs bara om när filen ändras. Sidan **CV** (`/cv`) visar hur CV:t
tolkades, så att du kan kontrollera det; sidan anropar aldrig Claude själv och är tom
tills ditt första utkast har skapats. Titlar, arbetsgivare, perioder, examina och skolor
som inte står ordagrant i `cv.pdf` visas som varningar där och på paketsidan.

För varje jobb väljer och formulerar Claude om profiltext, punkter och kompetenser efter
annonsen, på annonsens språk (svenska eller engelska). Titlar, arbetsgivare, orter och
perioder kopieras ordagrant från ditt CV; profiltext och punkter formuleras av Claude –
kontrollera varningarna. Koden kontrollerar svaret och visar **Varningar för CV:t** på
paketsidan, t.ex. när en kompetens som inte finns i ditt CV togs bort eller en siffra i en
punkt, i rubriken eller i profiltexten inte finns i originalet. Anställningar som Claude
utelämnar läggs till sist med originalpunkterna, så CV:t får aldrig luckor.

Misslyckas anpassningen (eller att skapa PDF:en) skapas utkastet ändå, och nedladdningen
ger ditt original-CV.
**Försök igen** gör ett nytt försök. Byter du `data/cv.pdf` efter att ett utkast skapats
ger nedladdningen det nya CV:t utan anpassning, med en varning; tryck **Försök igen** för
ett nytt skräddarsytt CV.

### Lägga till jobb från LinkedIn

Klistra in länkar till LinkedIn-jobb, en per rad, i rutan **Lägg till jobb från LinkedIn**
på startsidan och tryck **Lägg till**. Högst 20 länkar åt gången; rader som inte är
jobblänkar visas och hoppas över. Jobben hämtas och poängsätts i bakgrunden som en vanlig
sökning (syns under körningar), men går inte genom filtret i inställningarna eftersom du
har valt dem själv. Efteråt visar startsidan dina LinkedIn-jobb oavsett poäng, och
meddelandet säger hur många som lades till och hur många som redan fanns. Pågår redan en
sökning får du ett meddelande och texten finns kvar.
LinkedIn blockerar ibland sidor för den som inte är inloggad; jobbet läggs då till som
*ofullständig* med bara länken, och du kan öppna annonsen själv. Paketsidan varnar då att
brevet och CV:t bara bygger på titel och företag. Klistrar du in länken igen hämtas sidan på
nytt; går den att läsa nu uppdateras jobbet och poängsätts om (om du inte redan har börjat
ansöka).

### Starta automatiskt med launchd

```sh
uv run jobaplier install-launchd
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/se.jobaplier.plist
```

Agenten (`se.jobaplier`) startar tjänsten vid inloggning och startar om den om den
kraschar (`KeepAlive`). Loggar hamnar i `data/logs/jobaplier.log` och
`data/logs/jobaplier.err.log`. `PATH` i agenten innehåller katalogen där Claude Code
(`ai.claude_bin`) hittades när agenten installerades, plus `/opt/homebrew/bin`; kör
`install-launchd` igen om du flyttar Claude Code eller projektet. Agenten körs i din
användarsession så att Claude Codes inloggning (nyckelringen) är åtkomlig.

Stoppa och ta bort agenten:

```sh
launchctl bootout gui/$(id -u)/se.jobaplier
rm ~/Library/LaunchAgents/se.jobaplier.plist
```

Kör inte `jobaplier serve` för hand samtidigt som agenten är igång (porten är upptagen och
kommandot avbryts).

## Utvärdera prompterna

Lägg 5–10 riktiga annonser som `data/eval/*.json` (`{"title": …, "company": …,
"description": …}`) och kör:

```sh
uv run python scripts/eval_prompts.py
```

Skriptet poängsätter varje annons och skriver ett brev med den konfigurerade AI-backenden
och skriver ut poäng, motivering, brev, ordantal och påståenden utan stöd i CV:t.

## Tester

```sh
uv run pytest
```

Om WeasyPrint inte hittar pango-biblioteken på macOS (fel som `cannot load library 'libpango-1.0-0'`),
sätt `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` (testerna gör det automatiskt via `tests/conftest.py`).
