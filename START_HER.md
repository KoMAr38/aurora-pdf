# Start her

## Krav

- Windows 10 eller 11
- Microsoft Edge WebView2 Runtime. Den følger med Windows 11 og kommer med Edge-oppdateringene på Windows 10. Mangler den, sier installasjonssjekken fra og viser lenken
- Python 3.11–3.14. `1_INSTALLER.bat` finner den selv, eller tilbyr å installere Python 3.13 med winget
- Internett første gang, for å hente bibliotekene. Etterpå trengs ikke nett, heller ikke til OCR

## Filene

| Fil | Gjør | Tid |
|---|---|---|
| `1_INSTALLER.bat` | Finner Python, lager `.venv`, installerer bibliotekene, kjører installasjonssjekken, lager snarveier og tilbyr Aurora PDF for PDF-filer. Kan kjøres på nytt for å oppdatere | 1–3 min første gang |
| `START_MED_KONSOLL.bat` | Starter programmet med et konsollvindu som viser feilmeldinger. En PDF kan slippes på fila | |
| `2_TESTER.bat` | Installerer pytest og Playwright og kjører alle testene. Ende-til-ende-testene styrer Microsoft Edge | ~2 min |
| `3_BYGG_EXE.bat` | Lager en frittstående versjon i `dist\Aurora PDF\` med PyInstaller, til en PC uten Python | 1–2 min |
| `AVINSTALLER.bat` | Flytter snarveiene til papirkurven og fjerner Aurora PDF fra «Åpne med» og Standardapper. Programmappen og innstillingene blir liggende | |

Siste linje fra installasjonssjekken skal være:

```
All checks passed. Report: C:\Users\<bruker>\AppData\Roaming\AuroraPDF\selftest.txt
```

Siste linje fra `2_TESTER.bat` skal vise `269 passed`.

## Bruk

- Start fra snarveien «Aurora PDF» på skrivebordet eller i Start-menyen
- Åpne en fil med Ctrl+O, ved å dra den inn i vinduet, eller med «Åpne med» i Filutforsker hvis det ble valgt under installasjonen
- Skal PDF-filer åpnes i Aurora PDF med dobbeltklikk og vise Aurora-ikonet, velg Aurora PDF som standard i Innstillinger > Apper > Standardapper. Windows lar ikke programmer gjøre det selv. Installasjonen åpner den siden når PDF-spørsmålet besvares med J
- Språk og tema velges under ⋯ → Innstillinger. Programmet starter på serbisk første gang, og temaet følger Windows til et annet er valgt
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

- Vinduet er hvitt eller åpner seg ikke: WebView2 mangler. Installer «Microsoft Edge WebView2 Runtime» fra Microsoft og kjør `1_INSTALLER.bat` på nytt
- Programmet lukker seg eller en funksjon feiler: start med `START_MED_KONSOLL.bat` for å se feilmeldingen, eller les `aurora.log`
- En gammel `.venv` laget med en Python-versjon utenfor 3.11–3.14 blir flyttet til `_to_delete\` av `1_INSTALLER.bat`. Den mappen kan slettes
