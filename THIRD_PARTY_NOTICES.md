# Tredjepartskomponenter

Aurora PDF er lisensiert under GNU AGPL 3.0 (se `LICENSE`), fordi PDF-motoren PyMuPDF er det. Den som vil bruke koden i et lukket kommersielt produkt, trenger en kommersiell lisens for PyMuPDF fra Artifex.

## Python-pakker

Installeres fra PyPI av `1_INSTALLER.bat`. Lisensene følger med pakkene.

| Pakke | Versjon | Lisens | Brukes til |
|---|---|---|---|
| PyMuPDF (MuPDF) | 1.28.2 | AGPL-3.0 eller kommersiell lisens fra Artifex | Visning, redigering, kryptering, OCR med innebygd Tesseract |
| pywebview | 6.2.1 | BSD-3-Clause | Programvinduet (WebView2 på Windows) |
| pythonnet | 3.2.1 | MIT | Kobling mellom pywebview og .NET på Windows |
| pyHanko | 0.37.0 | MIT | Sertifikatsignaturer (PAdES) og validering |
| cryptography | 50.0.1 | Apache-2.0 eller BSD-3-Clause | Nøkler og sertifikater til digitale ID-er |
| pdf2docx | 0.5.13 | MIT | Eksport til Word |
| Pillow | 12.3.0 | MIT-CMU | JPEG-koding av sidebilder |

Pakkene over trekker inn blant annet python-docx, fonttools, numpy, opencv-python-headless, lxml, asn1crypto, aiohttp og tzlocal. De har egne lisenser (MIT, BSD, Apache-2.0) som følger med hver pakke.

Testene bruker i tillegg pytest (MIT) og Playwright (Apache-2.0).

## Filer som ligger i repoet

| Fil | Opphav | Lisens |
|---|---|---|
| `resources/fonts/NotoSans-Regular.ttf` | The Noto Project Authors | SIL Open Font License 1.1, `resources/fonts/OFL-NotoSans.txt` |
| `web/fonts/inter-*.woff2` | The Inter Project Authors | SIL Open Font License 1.1, `web/fonts/licenses/OFL-Inter.txt` |
| `web/fonts/space-grotesk-*.woff2` | The Space Grotesk Project Authors | SIL Open Font License 1.1, `web/fonts/licenses/OFL-SpaceGrotesk.txt` |
| `web/fonts/GreatVibes-Regular.ttf` | The Great Vibes Pro Project Authors | SIL Open Font License 1.1, `web/fonts/licenses/OFL-GreatVibes.txt` |
| `web/fonts/Caveat-Medium.ttf` | The Caveat Project Authors | SIL Open Font License 1.1, `web/fonts/licenses/OFL-Caveat.txt` |
| `web/fonts/MarckScript-Regular.ttf` | Denis Masharov og Marck Fogel | SIL Open Font License 1.1, `web/fonts/licenses/OFL-MarckScript.txt` |
| `resources/tessdata/*.traineddata` | tesseract-ocr/tessdata_fast (engelsk, norsk, serbisk kyrillisk og latinsk) | Apache-2.0, `resources/tessdata/LICENSE` |

Ikonene i `web/js/icons.js` og logoen i `web/assets/aurora.ico` er laget for dette programmet.

Aurora PDF er ikke tilknyttet Adobe eller andre PDF-leverandører.
