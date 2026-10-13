"""Sample documents for the end-to-end tests and the screenshots.

The report is a made-up quarterly report for a fictional company; it has real text,
a table, vector graphics, links, bookmarks, an attachment, a form and a scanned page.
"""
from __future__ import annotations

import io
import random
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
FONT = str(ROOT / "resources" / "fonts" / "NotoSans-Regular.ttf")

CSS = """
* { font-family: sans-serif; color: #1f2937; }
h1 { font-size: 26px; color: #0f172a; margin: 0 0 4px 0; }
h2 { font-size: 15px; color: #0f766e; margin: 18px 0 6px 0; }
p { font-size: 10.5px; line-height: 1.5; margin: 0 0 8px 0; }
.kicker { font-size: 9px; color: #0e7490; letter-spacing: 1px; }
.muted { color: #64748b; }
table { border-collapse: collapse; width: 100%; margin-top: 6px; }
th { font-size: 9.5px; text-align: left; color: #475569; border-bottom: 1px solid #94a3b8; padding: 4px 6px; }
td { font-size: 10px; padding: 5px 6px; border-bottom: 1px solid #e2e8f0; }
td.n, th.n { text-align: right; }
li { font-size: 10.5px; line-height: 1.5; }
"""

PAGE1 = """
<p class="kicker">NORDLYS ANALYSE AS · KVARTALSRAPPORT</p>
<h1>Tredje kvartal 2026</h1>
<p class="muted">Utarbeidet av økonomiavdelingen, Tromsø, oktober 2026</p>
<h2>Sammendrag</h2>
<p>Driftsinntektene økte med 14 prosent sammenlignet med samme kvartal i fjor, drevet av nye avtaler
innen energisektoren og høyere aktivitet i konsulentvirksomheten. Driftsmarginen ble 18,4 prosent.
Selskapet åpnet et nytt kontor i Bodø og ansatte elleve nye medarbeidere i perioden.</p>
<p>Styret vurderer utsiktene for fjerde kvartal som gode. Ordrereserven ved utgangen av kvartalet var
den høyeste i selskapets historie, og det forventes fortsatt vekst i offentlig sektor.</p>
<h2>Nøkkeltall</h2>
<table>
<tr><th>Mill. kr</th><th class="n">K3 2026</th><th class="n">K3 2025</th><th class="n">Endring</th></tr>
<tr><td>Driftsinntekter</td><td class="n">48,6</td><td class="n">42,6</td><td class="n">+14 %</td></tr>
<tr><td>Driftsresultat</td><td class="n">8,9</td><td class="n">6,8</td><td class="n">+31 %</td></tr>
<tr><td>Resultat før skatt</td><td class="n">8,4</td><td class="n">6,1</td><td class="n">+38 %</td></tr>
<tr><td>Antall ansatte</td><td class="n">96</td><td class="n">81</td><td class="n">+15</td></tr>
</table>
"""

PAGE2 = """
<h2>Marked og kunder</h2>
<p>Etterspørselen etter dataanalyse og rådgivning holdt seg sterk gjennom hele kvartalet. Andelen
inntekter fra gjentakende avtaler økte til 61 prosent. De tre største kundene sto for 27 prosent av
omsetningen, ned fra 33 prosent ett år tidligere.</p>
<ul>
<li>Ny rammeavtale med et regionalt energiselskap, verdi inntil 22 millioner kroner.</li>
<li>Utvidet samarbeid med to kommuner om beslutningsstøtte og rapportering.</li>
<li>Lansering av en egen tjeneste for bærekraftsrapportering.</li>
</ul>
<h2>Organisasjon</h2>
<p>Sykefraværet var 3,1 prosent. Selskapet har innført fleksible arbeidstider for alle avdelinger
og fullført opplæring i informasjonssikkerhet for samtlige ansatte.</p>
<h2>Risiko</h2>
<p>Den viktigste risikoen er tilgang på kvalifisert arbeidskraft. Selskapet har lav gjeld og god
likviditet, og valutarisikoen er begrenset.</p>
<p class="muted">Mer informasjon: https://example.com/rapporter</p>
"""

SCAN_TEXT = """Tromsø, 2. oktober 2026

Bekreftelse av mottatt dokumentasjon

Vi bekrefter med dette å ha mottatt kvartalsrapporten for tredje kvartal 2026
med tilhørende vedlegg. Dokumentene er gjennomgått og arkivert.

Eventuelle spørsmål kan rettes til revisjonsavdelingen.

Med vennlig hilsen
Kari Nordmann
Revisor"""


def _bars(page: pymupdf.Page, rect: pymupdf.Rect) -> None:
    """Small vector bar chart: revenue per quarter."""
    vals = [36.1, 39.4, 42.6, 44.0, 45.2, 47.1, 48.6]
    labels = ["K1", "K2", "K3", "K4", "K1", "K2", "K3"]
    shape = page.new_shape()
    shape.draw_line(rect.bl, rect.br)
    shape.finish(color=(0.58, 0.64, 0.72), width=0.6)
    n = len(vals)
    w = rect.width / n
    for i, v in enumerate(vals):
        h = (v / 52) * (rect.height - 14)
        r = pymupdf.Rect(rect.x0 + i * w + w * 0.22, rect.y1 - h, rect.x0 + (i + 1) * w - w * 0.22, rect.y1)
        shape.draw_rect(r)
        last = i == n - 1
        shape.finish(fill=(0.05, 0.58, 0.53) if last else (0.6, 0.85, 0.82), color=None, width=0)
    shape.commit()
    for i, lab in enumerate(labels):
        page.insert_text((rect.x0 + i * w + w * 0.28, rect.y1 + 11), lab, fontsize=7.5, color=(0.39, 0.45, 0.55))
    page.insert_text((rect.x0, rect.y0 - 6), "Driftsinntekter per kvartal (mill. kr)", fontsize=8.5,
                     color=(0.28, 0.33, 0.41))


def _scan_image(text: str, width_pt: float, height_pt: float) -> bytes:
    """A page of text rendered to a slightly noisy grayscale image, like a scan."""
    tmp = pymupdf.open()
    pg = tmp.new_page(width=width_pt, height=height_pt)
    pg.insert_font(fontname="noto", fontfile=FONT)
    y = 90
    for line in text.splitlines():
        size = 13 if line.startswith("Bekreftelse") else 11
        pg.insert_text((70, y), line, fontname="noto", fontsize=size)
        y += size * 1.75
    pix = pg.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    rnd = random.Random(7)
    samples = bytearray(pix.samples)
    for k in range(0, len(samples), 3):
        samples[k] = max(0, min(255, samples[k] - rnd.randint(0, 22)))
    noisy = pymupdf.Pixmap(pymupdf.csGRAY, pix.width, pix.height, bytes(samples), False)
    return noisy.tobytes("png")


def build_report(path: str | Path) -> str:
    """Four-page sample report; returns the path."""
    doc = pymupdf.open()
    W, H = 595, 842
    # page 1 - summary, key figures, chart
    p1 = doc.new_page(width=W, height=H)
    p1.insert_htmlbox(pymupdf.Rect(60, 60, W - 60, 520), PAGE1, css=CSS)
    _bars(p1, pymupdf.Rect(70, 480, W - 70, 620))
    # page 2 - text, list, links
    p2 = doc.new_page(width=W, height=H)
    p2.insert_htmlbox(pymupdf.Rect(60, 60, W - 60, H - 80), PAGE2, css=CSS)
    hits = p2.search_for("https://example.com/rapporter")
    if hits:
        p2.insert_link({"kind": pymupdf.LINK_URI, "from": hits[0], "uri": "https://example.com/rapporter"})
    # page 3 - a form
    p3 = doc.new_page(width=W, height=H)
    p3.insert_htmlbox(pymupdf.Rect(60, 60, W - 60, 200),
                      "<h2>Tilbakemeldingsskjema</h2><p>Fyll ut skjemaet og signer nederst på siden.</p>", css=CSS)
    labels = [("Navn", 150), ("E-post", 200), ("Avdeling", 250)]
    for text, y in labels:
        p3.insert_text((62, y + 14), text, fontsize=10, color=(0.28, 0.33, 0.41))
    for name, y in (("navn", 150), ("epost", 200)):
        w = pymupdf.Widget()
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        w.rect = pymupdf.Rect(150, y, 450, y + 22)
        w.text_fontsize = 11
        p3.add_widget(w)
    combo = pymupdf.Widget()
    combo.field_type = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
    combo.field_name = "avdeling"
    combo.choice_values = ["Analyse", "Rådgivning", "Økonomi", "Ledelse"]
    combo.field_value = "Analyse"
    combo.rect = pymupdf.Rect(150, 250, 450, 272)
    combo.text_fontsize = 11
    p3.add_widget(combo)
    cb = pymupdf.Widget()
    cb.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    cb.field_name = "samtykke"
    cb.rect = pymupdf.Rect(62, 305, 76, 319)
    p3.add_widget(cb)
    p3.insert_text((84, 316), "Jeg bekrefter at opplysningene er riktige.", fontsize=10)
    p3.insert_text((62, 380), "Signatur", fontsize=10, color=(0.28, 0.33, 0.41))
    sig = pymupdf.Widget()
    sig.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    sig.field_name = "signatur"
    sig.rect = pymupdf.Rect(150, 360, 400, 420)
    p3.add_widget(sig)
    # page 4 - a scanned letter (image only, no text layer)
    p4 = doc.new_page(width=W, height=H)
    p4.insert_image(p4.rect, stream=_scan_image(SCAN_TEXT, W, H))
    # outline, attachment, metadata
    doc.set_toc([[1, "Sammendrag", 1], [2, "Nøkkeltall", 1], [1, "Marked og kunder", 2], [1, "Risiko", 2],
                 [1, "Skjema", 3], [1, "Bekreftelse fra revisor", 4]])
    doc.embfile_add("nokkeltall.csv", "Post;K3 2026;K3 2025\nDriftsinntekter;48,6;42,6\n".encode("utf-8"),
                    filename="nokkeltall.csv", desc="Nøkkeltall")
    doc.set_metadata({"title": "Kvartalsrapport K3 2026", "author": "Nordlys Analyse AS",
                      "subject": "Kvartalsrapport", "keywords": "rapport, kvartal, 2026",
                      "creator": "Aurora PDF sample", "producer": "PyMuPDF"})
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()
    return str(path)


def build_plain(path: str | Path, pages: int = 3) -> str:
    """Simple document with one marker line per page (for quick checks)."""
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=420, height=595)
        pg.insert_font(fontname="noto", fontfile=FONT)
        pg.insert_text((50, 80), f"Aurora sample page {i + 1}", fontname="noto", fontsize=18)
        pg.insert_text((50, 110), f"marker-{i + 1} lorem ipsum dolor sit amet", fontname="noto", fontsize=12)
        pg.insert_text((50, 130), "Čačak Šabac Đurđevdan Tromsø", fontname="noto", fontsize=12)
    doc.save(str(path))
    doc.close()
    return str(path)


def build_big(path: str | Path, pages: int = 300) -> str:
    """Long text document for scrolling checks."""
    doc = pymupdf.open()
    para = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore "
            "et dolore magna aliqua. ") * 3
    for i in range(pages):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((60, 70), f"Page {i + 1}", fontsize=20)
        pg.insert_textbox(pymupdf.Rect(60, 100, 535, 780), (para + "\n\n") * 6, fontsize=10.5)
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()
    return str(path)


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "report.pdf"
    print(build_report(out))
