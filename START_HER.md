# Start her

## Installere

Fra Microsoft Store, eller med `install.exe`: [last den ned her](https://github.com/KoMAr38/aurora-pdf/releases/latest/download/install.exe). Er du i prosjektmappen etter `scripts\bygg.bat`, ligger `install.exe` og `uninstall.exe` rett i den.

1. Kjør `install.exe`. Den kopierer programmet til `%LOCALAPPDATA%\Programs\Aurora PDF`, sjekker at alt virker, lager snarveier på skrivebordet og i Start-menyen, legger Aurora PDF til under «Åpne med» og starter programmet. Ingen spørsmål, ingen administratorrettigheter, ikke noe nett
2. Ferdig

Fjerne programmet: kjør `uninstall.exe`, eller velg Aurora PDF i Innstillinger > Apper. Innstillinger, signaturer og digitale ID-er i `%APPDATA%\AuroraPDF\` blir liggende.

Oppdatere: programmet ser selv etter en ny versjon når det starter og hver sjette time. Finnes det en, blir oppdateringsknappen øverst til høyre grønn og en linje sier fra. Trykk «Last ned og installer», eller på knappen når som helst: `install.exe` lastes ned, programmet lukkes, og den nye versjonen starter. Store-kopien oppdateres av Store og har ikke den knappen.

Windows SmartScreen kan si «Windows beskyttet PC-en» første gang, fordi filen ikke er kodesignert. Velg «Mer informasjon» og «Kjør likevel».

## Krav

- Windows 10 eller 11
- Microsoft Edge WebView2 Runtime. Den følger med Windows 11 og kommer med Edge-oppdateringene på Windows 10. Mangler den, sier installasjonen fra og viser lenken

## Bruk

- Start fra snarveien «Aurora PDF» på skrivebordet eller i Start-menyen
- Åpne en fil med Ctrl+O, ved å dra den inn i vinduet, eller med «Åpne med» i Filutforsker
- Skal PDF-filer åpnes i Aurora PDF med dobbeltklikk, velg Aurora PDF som standard i Innstillinger > Apper > Standardapper. Windows lar ikke programmer gjøre det selv
- Språk og tema velges under ⋯ → Innstillinger. Første gang følger språket Windows: norsk, serbisk (også for kroatisk og bosnisk) eller ellers engelsk. Temaet følger Windows til et annet er valgt
- `?` viser alle hurtigtastene

## Hvor ting lagres

Alt ligger i `%APPDATA%\AuroraPDF\`:

| Hva | Hvor |
|---|---|
| Innstillinger og nylig brukte filer | `settings.json` |
| Lagrede signaturer | `signatures\`, og fjernede signaturer i `signatures\removed\` |
| Digitale ID-er, låst med passordet ditt | `ids\`, og fjernede ID-er i `ids\removed\` |
| Logg | `aurora.log` |
| Rapport fra installasjonssjekken | `selftest.txt` |

## Når noe ikke virker

- Installasjonen stopper med «Installasjonssjekken fant feil»: les `selftest.txt`. Står det WebView2, installer «Microsoft Edge WebView2 Runtime» fra Microsoft og kjør `install.exe` på nytt
- En funksjon feiler: les `aurora.log`

## For utviklere

Krever Python 3.11–3.14.

| Hva | Kommando |
|---|---|
| Kjøre fra kildekoden | `.venv\Scripts\python.exe main.py` |
| Installasjonssjekken | `.venv\Scripts\python.exe main.py --selftest`. Siste linje skal være `All checks passed.` |
| Testene (pytest og Playwright) | `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`, så `.venv\Scripts\python.exe -m pytest`. Siste linje skal vise `504 passed, 10 skipped`. Ni av dem som hoppes over, gjelder språket ved første start på andre systemer enn Windows, og én trenger skriften DejaVu Sans |
| Lage `install.exe` og `uninstall.exe` | `scripts\bygg.bat`. Lager `.venv` om den mangler, kjører installasjonssjekken og legger de to filene rett i prosjektmappen |
| Legge ut en ny versjon | Hev versjonen i `core\paths.py` og `packaging\AppxManifest.xml`, og kjør `scripts\utgivelse.bat`. Den bygger og legger `install.exe` og `uninstall.exe` ut som `v<versjon>` på GitHub, der oppdateringsknappen i programmet finner dem. Krever GitHub CLI (`gh auth login`) |
| Store-pakken | Bygges på GitHub når grenen `store` sendes opp (`.github/workflows/store.yml`) |

Ende-til-ende-testene bruker Microsoft Edge når den finnes, ellers Playwrights egen Chromium (`.venv\Scripts\python.exe -m playwright install --only-shell chromium`).
