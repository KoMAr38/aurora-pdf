"""Screenshots for the README (docs/bilder), with the UI in Norwegian. Run by hand:

    python tests/e2e/readme_images.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import app_server, browser_page, open_pdf, rpc, script_dialogs, settle  # noqa: E402
import samples  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "bilder"


def at(page, i, x, y):
    """Screen position of PDF point (x, y) on page i."""
    return page.evaluate("""([i, x, y]) => { const pg = Aurora.V.pages[i]; const r = pg.el.getBoundingClientRect();
      return [r.left + x / pg.w * r.width, r.top + y / pg.h * r.height]; }""", [i, x, y])


def drag(page, i, points, steps=6):
    page.mouse.move(*at(page, i, *points[0]))
    page.mouse.down()
    for p in points[1:]:
        page.mouse.move(*at(page, i, *p), steps=steps)
    page.mouse.up()


def word(page, i, text, nth=0):
    """Centre of the nth word on page i that equals text."""
    box = page.evaluate("""([i, t, n]) => { const ws = Aurora.V.pages[i].info.words.filter(w => w[5] === t);
      return Aurora.wordBox(ws[n]); }""", [i, text, nth])
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def show(page, i):
    page.evaluate("(i) => Aurora.gotoPage(i, false)", i)
    page.wait_for_function("(i) => Aurora.V.pages[i].mat && Aurora.V.pages[i].info", arg=i, timeout=20000)
    settle(page, 500)


def panel(page, name):
    page.evaluate("(n) => Aurora.openPanel(n)", name)
    settle(page, 900)


def shot(page, name):
    settle(page, 900)
    page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")
    page.screenshot(path=str(OUT / name))
    print("wrote", OUT / name)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    report = samples.build_report(tmp / "kvartalsrapport.pdf")
    with app_server() as (url, _), browser_page(url) as page:
        page.evaluate("Aurora.setLang('no')")
        rpc(page, "settings_set", {"author": "Kari Nordmann"})
        open_pdf(page, report)

        # comments drawn with the mouse, listed in the panel
        panel(page, "annotate")
        page.click('#atb [data-mode="highlight"]')
        drag(page, 0, [word(page, 0, "Driftsinntektene"), word(page, 0, "prosent")])
        settle(page)
        page.click('#atb [data-mode="pen"]')
        page.click('#atb .sw[data-c="#2563eb"]')
        cx, cy = word(page, 0, "+31")
        drag(page, 0, [(cx - 2, cy - 12), (cx + 22, cy - 9), (cx + 30, cy + 2), (cx + 18, cy + 11),
                       (cx - 14, cy + 11), (cx - 26, cy + 1), (cx - 16, cy - 10), (cx + 4, cy - 13)])
        settle(page)
        page.click('#atb [data-mode="rect"]')
        _, ry = word(page, 0, "Driftsresultat")
        drag(page, 0, [(64, ry - 10), (532, ry + 10)])
        settle(page)
        page.click('#atb [data-mode="note"]')
        page.mouse.click(*at(page, 0, 548, 236))
        page.wait_for_selector(".scrim textarea")
        page.fill(".scrim textarea", "Sjekk tallet mot årsregnskapet.")
        page.locator(".scrim:not(.out) .m-foot .btn").last.click()
        settle(page)
        page.click('#atb [data-mode="select"]')
        page.mouse.click(5, 400)
        shot(page, "oversikt.png")

        # form filled in, then signed with a certificate into the empty signature field
        panel(page, None)
        show(page, 2)
        page.fill('.pg[data-i="2"] .fx input[title="navn"]', "Kari Nordmann")
        page.press('.pg[data-i="2"] .fx input[title="navn"]', "Tab")
        page.fill('.pg[data-i="2"] .fx input[title="epost"]', "kari@nordlys.example")
        page.press('.pg[data-i="2"] .fx input[title="epost"]', "Tab")
        settle(page)
        page.locator('.pg[data-i="2"] .fx select[title="avdeling"]').select_option("Økonomi")
        settle(page)
        page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]').click()
        settle(page)
        rpc(page, "id_create", {"name": "Kari Nordmann", "password": "abcd1234", "email": "kari@nordlys.example"})
        script_dialogs(page, [str(tmp / "kvartalsrapport-signert.pdf")])
        page.click('.pg[data-i="2"] .fx .sigfield')
        page.wait_for_selector("#dsPw")
        page.fill("#dsPw", "abcd1234")
        page.fill("#dsR", "Godkjent")
        page.fill("#dsL", "Tromsø")
        page.locator(".scrim:not(.out) .m-foot .btn").last.click()
        page.wait_for_function("Aurora.S.doc && Aurora.S.doc.signed", timeout=30000)
        show(page, 2)
        panel(page, "fillsign")
        page.wait_for_selector(".sigstat", timeout=20000)
        page.evaluate("document.querySelector('.sigstat').scrollIntoView({block: 'end'})")
        page.evaluate("Aurora.setTheme('light')")  # one picture in the light theme
        shot(page, "signatur.png")
        page.evaluate("Aurora.setTheme('system')")

        # password protection
        open_pdf(page, report)
        panel(page, "protect")
        page.fill("#userPw", "nordlys-2026")
        page.fill("#ownerPw", "eier-2026")
        page.uncheck("#p_copy", force=True)
        page.uncheck("#p_modify", force=True)
        page.mouse.click(5, 400)
        shot(page, "passord.png")

        # pages
        panel(page, "organize")
        page.locator("#grid .tile[data-i]").nth(1).click()
        page.locator("#grid .tile[data-i]").nth(2).click(modifiers=["Control"])
        shot(page, "sider.png")

        # a scanned page made searchable
        panel(page, None)
        show(page, 3)
        panel(page, "ocr")
        page.click("#ocrGo")
        page.wait_for_function("Aurora.V.pages[3].info && Aurora.V.pages[3].info.words.length > 20", timeout=60000)
        settle(page, 1200)
        drag(page, 3, [word(page, 3, "Vi"), word(page, 3, "2026", 1)], steps=10)
        shot(page, "ocr.png")
        print("errors:", page.errors)


if __name__ == "__main__":
    main()
