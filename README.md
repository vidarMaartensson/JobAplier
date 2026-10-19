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
något saknas (CV, inställningar, Claude Code eller API-nyckel, Gmail-inloggning) visas
installationssidan `/setup` med en lista över vad som återstår. Inställningarna kan fyllas
i under **Inställningar** i dashboarden; de sparas i `data/config.yaml` och schemat läggs om
direkt. Ändrar du `data/config.yaml` för hand medan tjänsten kör, starta om den.

## Gmail-inloggning

Gmail används för att skicka mejlansökningar och läsa LinkedIn-aviseringar
(scopes `gmail.send` och `gmail.readonly`).

1. Skapa ett projekt i [Google Cloud Console](https://console.cloud.google.com/), aktivera
   Gmail API och konfigurera OAuth-samtyckesskärmen (typ *External*, lägg till dig själv som
   testanvändare).
2. Skapa ett OAuth-klient-id av typen *Desktop app* och spara JSON-filen som
   `data/credentials.json`.
3. Logga in (öppnar webbläsaren):

   ```sh
   uv run jobaplier gmail-login
   ```

   Token sparas i `data/gmail_token.json` (bara läsbar för dig). Om inloggningen går ut
   visar dashboarden en varning; kör kommandot igen, tjänsten behöver inte startas om.

## LinkedIn-aviseringar

JobAplier loggar aldrig in på LinkedIn. Skapa i stället jobbaviseringar på LinkedIn
(sök → *Bevaka sökning*/*Set alert*, gärna dagligen) så att de mejlas till samma
Gmail-konto. Varje körning läser nya aviseringsmejl från `jobalerts-noreply@linkedin.com`
och hämtar de publika jobbsidorna. Saknas Gmail-inloggningen hoppas källan över och
körningen visar ett fel i dashboarden. Stäng av källan med
`sources.linkedin_alerts.enabled: false`.

## Drift

| Kommando | Vad det gör |
|---|---|
| `uv run jobaplier serve` | Dashboard på `http://127.0.0.1:8000` och schemalagda sökningar (standard 08:00, 12:00, 17:00, Europe/Stockholm). |
| `uv run jobaplier run-once` | En sökkörning direkt, skriver ut statistik och fel. Inte samtidigt som `serve` (se nedan). |
| `uv run jobaplier gmail-login` | Gmail-inloggning (se ovan). |
| `uv run jobaplier install-launchd` | Skriver launchd-agenten (se nedan). |

Kör inte `jobaplier run-once` medan `jobaplier serve` (eller launchd-agenten) är igång.
Spärren för "en körning åt gången" gäller bara inom en process, så två processer kan söka
och poängsätta samtidigt mot samma databas. Använd *Sök nu* i dashboarden i stället, eller
stoppa tjänsten först.

Missade schemalagda körningar (datorn sov) görs när datorn vaknar, om det är högst sex
timmar sedan; flera missade körningar slås ihop till en. "Sök nu" i dashboarden startar en
körning direkt; bara en körning åt gången.

`apply.dry_run: true` (standard) skickar alla mejlansökningar till din egen adress med
ämnet `[TORRKÖRNING → mottagare] …`. Jobbet ligger sedan kvar som utkast, så att du kan
skicka det på riktigt när torrkörningen är avstängd. Formulär fylls i i en synlig
Chromium men skickas aldrig av JobAplier – du granskar och klickar själv på Skicka.

Avbryts tjänsten mitt i ett mejlutskick markeras ansökan som misslyckad vid nästa start
("Avbröts under utskick – kontrollera Skickat i Gmail innan du försöker igen"). Brev som
höll på att skrivas blir poängsatta jobb igen, och en körning som avbröts markeras som
avbruten.

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
