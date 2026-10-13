"""End-to-end: the real app server and the real UI in Chromium, driven like a user would.

Mouse drags draw on pages, keyboard shortcuts run commands, native file dialogs are
answered through the test-mode hook. Skipped when Playwright is not installed.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import pymupdf
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from harness import app_server, launch_browser, rpc, script_dialogs  # noqa: E402
import samples  # noqa: E402

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def server():
    with app_server() as (url, data):
        yield url, data


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = launch_browser(pw)
        yield b
        b.close()


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    return {
        "report": samples.build_report(d / "report.pdf"),
        "plain": samples.build_plain(d / "plain.pdf"),
    }


class UI:
    """A small vocabulary for talking to the page."""

    def __init__(self, page, tmp: Path, data_dir: str, docs: dict):
        self.page, self.tmp, self.data, self.docs = page, tmp, data_dir, docs

    # -- state
    def js(self, expr: str, arg=None):
        return self.page.evaluate(expr, arg) if arg is not None else self.page.evaluate(expr)

    def doc(self) -> dict:
        return self.js("Aurora.S.doc")

    def idle(self, ms: int = 300) -> None:
        self.js("Aurora.commitPending()")
        self.page.wait_for_function("() => !document.querySelector('#busy.show')", timeout=30000)
        self.page.wait_for_timeout(ms)

    def rpc(self, method: str, payload: dict | None = None) -> dict:
        return rpc(self.page, method, payload)

    def annots(self, page_no: int | None = None) -> list[dict]:
        items = self.rpc("annots_list")["items"]
        return [a for a in items if page_no is None or a["p"] == page_no]

    def toasts(self) -> list[str]:
        return self.js("[...document.querySelectorAll('.toast .msg')].map(e => e.textContent)")

    def dialogs(self, *answers) -> None:
        script_dialogs(self.page, [str(a) if isinstance(a, Path) else a for a in answers])

    # -- documents
    def open(self, name_or_path: str, copy: bool = True) -> str:
        src = self.docs.get(name_or_path, name_or_path)
        path = src
        if copy:
            path = str(self.tmp / os.path.basename(src))
            shutil.copy(src, path)
        self.js("(p) => Aurora.openPath(p, {skipGuard: true})", path)
        self.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=path, timeout=20000)
        self.wait_pages()
        return path

    def wait_pages(self) -> None:
        self.page.wait_for_selector(".pg img.hi.ready", timeout=20000)
        self.page.wait_for_function("() => Aurora.V.pages.some(p => p.info)", timeout=20000)
        self.page.wait_for_timeout(250)

    # -- geometry: PDF display points of page i -> screen
    def box(self, i: int) -> dict:
        return self.js("(i) => { const r = Aurora.V.pages[i].el.getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height, pw: Aurora.V.pages[i].w, ph: Aurora.V.pages[i].h}; }", i)

    def at(self, i: int, x: float, y: float) -> tuple[float, float]:
        b = self.box(i)
        return b["x"] + x / b["pw"] * b["w"], b["y"] + y / b["ph"] * b["h"]

    def drag(self, i: int, pts: list[tuple[float, float]], steps: int = 6) -> None:
        m = self.page.mouse
        x, y = self.at(i, *pts[0])
        m.move(x, y)
        m.down()
        for p in pts[1:]:
            x, y = self.at(i, *p)
            m.move(x, y, steps=steps)
        m.up()

    def click_at(self, i: int, x: float, y: float) -> None:
        self.page.mouse.click(*self.at(i, x, y))

    def word_box(self, i: int, text: str) -> list[float]:
        """Display-space box of the first word on page i that equals text."""
        return self.js("""([i, t]) => {
          const info = Aurora.V.pages[i].info; const k = info.words.findIndex(w => w[5] === t);
          return k < 0 ? null : Aurora.wordBox(info.words[k]); }""", [i, text])

    def show_page(self, i: int) -> None:
        self.js("(i) => Aurora.gotoPage(i, false)", i)
        self.page.wait_for_function("(i) => Aurora.V.pages[i].mat && Aurora.V.pages[i].info", arg=i, timeout=20000)
        self.page.wait_for_timeout(250)

    # -- UI pieces
    def tool(self, mode: str) -> None:
        if self.js("Aurora.S.panel") != "annotate":
            self.page.click('#rail [data-tool="annotate"]')
            self.settle_layout()
        self.page.click(f'#atb [data-mode="{mode}"]')
        assert self.js("Aurora.S.mode") == mode

    def settle_layout(self) -> None:
        """Wait until panels finished sliding and the pages were refitted."""
        last = None
        for _ in range(40):
            now = self.js("() => { const p = Aurora.V.pages[0]; const r = (p ? p.el : document.querySelector('#viewer')).getBoundingClientRect(); return [r.left, r.top, r.width, Aurora.S.zoom]; }")
            if now == last:
                return
            last = now
            self.page.wait_for_timeout(150)

    def panel(self, name: str) -> None:
        self.page.click(f'#rail [data-tool="{name}"]')
        self.settle_layout()

    def modal_button(self, label: str) -> None:
        self.page.locator(".scrim:not(.out) .m-foot .btn", has_text=label).last.click()

    def modal_open(self) -> bool:
        return self.page.locator(".scrim:not(.out)").count() > 0

    def t(self, key: str) -> str:
        return self.js("(k) => Aurora.t(k)", key)


@pytest.fixture
def ui(server, browser, docs, tmp_path):
    url, data = server
    # "dark" plays a Windows set to dark apps; the theme follows it until one is picked
    ctx = browser.new_context(viewport={"width": 1380, "height": 860}, accept_downloads=False, color_scheme="dark")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and errors.append(f"console: {m.text}"))
    page.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status} {r.url}"))
    page.goto(url)
    page.wait_for_selector("body.ready", timeout=20000)
    u = UI(page, tmp_path, data, docs)
    u.errors = errors
    yield u
    found = list(errors)  # what went wrong during the test itself
    try:
        rpc(page, "test_dialogs", {"answers": []})
        rpc(page, "close_doc")
        rpc(page, "settings_set", {"lang": "sr", "theme": "system", "night": False, "fit": "width"})
    finally:
        ctx.close()
    assert not found, found


# ---------------------------------------------------------------- viewing

def test_open_shows_pages_thumbnails_and_info(ui):
    ui.open("report")
    d = ui.doc()
    assert len(d["pages"]) == 4 and d["forms"] and not d["signed"]
    assert ui.page.locator("#thumbs .tile").count() == 4
    assert ui.page.inner_text("#fileInfo .name") == "report.pdf"
    assert "/ 4" in ui.page.inner_text("#vbar .pnum")
    # the text layer has the words of page 1, in reading order
    words = ui.js("[...document.querySelectorAll('.pg[data-i=\"0\"] .tl span')].map(s => s.textContent.trim())")
    assert "Driftsinntektene" in words and "Nøkkeltall" in words


def test_long_document_keeps_only_nearby_pages_alive(ui, tmp_path):
    big = samples.build_big(tmp_path / "big.pdf", pages=240)
    ui.open(big, copy=False)
    assert ui.js("Aurora.V.pages.length") == 240
    assert ui.js("Aurora.V.pages.filter(p => p.mat).length") <= 12
    ui.js("Aurora.gotoPage(200, false)")
    ui.page.wait_for_function("Aurora.V.pages[200].mat && Aurora.V.pages[200].info", timeout=20000)
    assert ui.js("Aurora.S.current") == 200
    assert ui.page.input_value("#pageIn") == "201"
    alive = ui.js("Aurora.V.pages.map((p, i) => p.mat ? i : -1).filter(i => i >= 0)")
    assert len(alive) <= 12 and all(185 <= i <= 215 for i in alive)
    # sharp image for the visible page arrives at device width
    ui.page.wait_for_function("Aurora.V.pages[200].hi.classList.contains('ready')", timeout=20000)


def test_zoom_buttons_wheel_and_fit(ui):
    ui.open("plain")
    z0 = ui.js("Aurora.S.zoom")
    ui.page.click('#vbar [data-v="in"]')
    assert ui.js("Aurora.S.zoom") == pytest.approx(z0 * 1.2, rel=1e-3)
    assert ui.js("Aurora.S.fit") is None
    ui.page.mouse.move(700, 400)
    ui.page.keyboard.down("Control")
    ui.page.mouse.wheel(0, -300)
    ui.page.keyboard.up("Control")
    ui.page.wait_for_timeout(200)
    assert ui.js("Aurora.S.zoom") > z0 * 1.2
    ui.page.click('#vbar [data-v="fitp"]')
    assert ui.js("Aurora.S.fit") == "page"
    b = ui.box(0)
    vh = ui.js("document.querySelector('#viewer').clientHeight")
    assert b["h"] <= vh
    ui.page.keyboard.press("Control+0")
    assert ui.js("Aurora.S.fit") == "width"
    assert ui.page.inner_text("#zoomVal").endswith("%")


def test_search_highlights_and_steps_through_hits(ui):
    ui.open("report")
    ui.page.click("#search")
    ui.page.keyboard.type("prosent")
    ui.page.wait_for_function("Aurora.S.search.hits.length > 0", timeout=10000)
    n = ui.js("Aurora.S.search.hits.length")
    assert n >= 3
    assert ui.page.inner_text("#searchCount") == f"1/{n}"
    assert ui.page.locator(".ov .hit.cur").count() == 1
    ui.page.keyboard.press("Enter")
    assert ui.page.inner_text("#searchCount") == f"2/{n}"
    ui.page.keyboard.press("Shift+Enter")
    assert ui.page.inner_text("#searchCount") == f"1/{n}"
    ui.page.keyboard.press("Escape")
    assert ui.js("Aurora.S.search.hits.length") == 0
    assert ui.page.locator(".ov .hit").count() == 0


# ---------------------------------------------------------------- drawing and markup

def test_pen_draws_ink_annotation(ui):
    ui.open("plain")
    ui.tool("pen")
    ui.drag(0, [(80, 200), (140, 240), (200, 210), (260, 260)])
    ui.drag(0, [(80, 300), (260, 320)])
    ui.idle(1200)  # strokes are batched into one annotation
    ink = [a for a in ui.annots(0) if a["t"] == "Ink"]
    assert len(ink) == 1
    x0, y0, x1, y1 = ink[0]["r"]
    assert x0 < 90 and x1 > 250 and y0 < 205 and y1 > 315
    assert ui.doc()["undo"] == "ink" and ui.doc()["dirty"]
    assert ui.page.locator(".ov .pre-ink").count() == 0  # previews give way to the real rendering


@pytest.mark.parametrize("mode,kind", [("rect", "Square"), ("ellipse", "Circle"), ("line", "Line"), ("arrow", "Line")])
def test_shapes(ui, mode, kind):
    ui.open("plain")
    ui.tool(mode)
    ui.drag(0, [(100, 200), (250, 300)])
    ui.idle()
    shapes = [a for a in ui.annots(0) if a["t"] == kind]
    assert len(shapes) == 1
    x0, y0, x1, y1 = shapes[0]["r"]
    assert abs(x0 - 100) < 12 and abs(y1 - 300) < 12
    assert ui.doc()["undo"] == mode


@pytest.mark.parametrize("mode,kind", [("highlight", "Highlight"), ("underline", "Underline"), ("strikeout", "StrikeOut")])
def test_markup_by_dragging_over_words(ui, mode, kind):
    ui.open("plain")
    ui.tool(mode)
    a = ui.word_box(0, "marker-1")
    b = ui.word_box(0, "ipsum")
    ui.drag(0, [((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)])
    ui.idle()
    marks = [x for x in ui.annots(0) if x["t"] == kind]
    assert len(marks) == 1
    r = marks[0]["r"]
    assert r[0] <= a[0] + 1 and r[2] >= b[2] - 1


def test_text_selection_bar_highlights_selected_words(ui):
    ui.open("plain")
    a = ui.word_box(0, "Aurora")
    b = ui.word_box(0, "page")
    ui.page.mouse.move(*ui.at(0, a[0] + 1, (a[1] + a[3]) / 2))
    ui.page.mouse.down()
    ui.page.mouse.move(*ui.at(0, b[2] - 1, (b[1] + b[3]) / 2), steps=8)
    ui.page.mouse.up()
    ui.page.wait_for_selector("#selbar:not(.hidden)")
    assert "Aurora" in ui.js("window.getSelection().toString()")
    ui.page.click('#selbar [data-act="highlight"]')
    ui.idle()
    assert [x["t"] for x in ui.annots(0)] == ["Highlight"]


def test_text_box_and_sticky_note(ui):
    ui.open("plain")
    ui.tool("text")
    ui.click_at(0, 80, 220)
    # the editor takes focus one tick after it appears; typed letters before that are tool shortcuts
    ui.page.wait_for_function("document.activeElement && document.activeElement.matches('.ft-edit')", timeout=5000)
    ui.page.keyboard.type("Godkjent – Šabac")
    ui.page.keyboard.press("Control+Enter")
    ui.idle()
    ft = [a for a in ui.annots(0) if a["t"] == "FreeText"]
    assert len(ft) == 1 and ft[0]["txt"] == "Godkjent – Šabac"
    ui.tool("note")
    ui.click_at(0, 300, 260)
    ui.page.wait_for_selector(".scrim textarea")
    ui.page.fill(".scrim textarea", "Pogledaj ovo")
    ui.modal_button(ui.t("add"))
    ui.idle()
    notes = [a for a in ui.annots(0) if a["t"] == "Text"]
    assert len(notes) == 1 and notes[0]["txt"] == "Pogledaj ovo"
    # the comments panel lists both
    ui.page.wait_for_function("document.querySelectorAll('#clist .citem').length === 2", timeout=10000)


def test_select_move_recolor_delete_and_undo(ui):
    ui.open("plain")
    ui.tool("rect")
    ui.drag(0, [(100, 200), (200, 260)])
    ui.idle()
    ui.tool("select")
    ui.click_at(0, 150, 201)  # on the border region (hit box covers the annotation)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.drag(0, [(150, 201), (190, 251)], steps=8)
    ui.idle()
    r = ui.annots(0)[0]["r"]
    assert abs(r[0] - 140) < 6 and abs(r[1] - 250) < 6
    assert ui.doc()["undo"] == "move"
    # recolor from the mini toolbar
    ui.click_at(0, 190, 251)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.page.click('#annbar .sw[data-c="#2563eb"]')
    ui.idle()
    assert ui.annots(0)[0]["c"].lower() == "#2563eb"
    # delete with the keyboard, then undo twice
    ui.click_at(0, 190, 251)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.page.keyboard.press("Delete")
    ui.idle()
    assert ui.annots(0) == []
    ui.page.keyboard.press("Control+z")
    ui.idle()
    assert len(ui.annots(0)) == 1
    ui.page.keyboard.press("Control+y")
    ui.idle()
    assert ui.annots(0) == []


def test_quick_successive_drawings_all_land(ui):
    """The page stays drawable while its overlay reloads after each edit."""
    ui.open("plain")
    ui.tool("pen")
    ui.drag(0, [(60, 160), (120, 190), (180, 170)])
    ui.page.click('#atb [data-mode="rect"]')
    ui.drag(0, [(60, 210), (160, 260)])
    ui.drag(0, [(200, 210), (300, 260)])
    ui.page.click('#atb [data-mode="line"]')
    ui.drag(0, [(60, 300), (300, 320)])
    ui.idle(600)
    assert sorted(a["t"] for a in ui.annots(0)) == ["Ink", "Line", "Square", "Square"]


def test_eraser_removes_clicked_annotation(ui):
    ui.open("plain")
    ui.tool("ellipse")
    ui.drag(0, [(100, 300), (220, 380)])
    ui.idle()
    ui.tool("eraser")
    ui.click_at(0, 160, 340)
    ui.idle()
    assert ui.annots(0) == []


# ---------------------------------------------------------------- forms and signatures

def test_fill_form_fields(ui):
    ui.open("report")
    ui.show_page(2)
    field = ui.page.locator('.pg[data-i="2"] .fx input[title="navn"]')
    field.fill("Marko Petrović")
    field.press("Tab")
    ui.idle()
    ui.page.locator('.pg[data-i="2"] .fx select[title="avdeling"]').select_option("Økonomi")
    ui.idle()
    chk = ui.page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]')
    chk.click()
    ui.idle()
    # the tick shows while the box still has the focus (it used to wait for a click elsewhere)
    assert "on" in chk.get_attribute("class").split()
    vals = ui.js("Aurora.V.pages[2].info.fields.reduce((o, f) => (o[f.n] = f.v, o), {})")
    assert vals["navn"] == "Marko Petrović" and vals["avdeling"] == "Økonomi" and vals["samtykke"]
    assert ui.doc()["undo"] == "form"
    chk.click()
    ui.idle()
    assert "on" not in chk.get_attribute("class").split()
    assert not ui.js("Aurora.V.pages[2].info.fields.find(f => f.n === 'samtykke').v")
    # the checkbox sits exactly on its field (a shared class name once pushed it 10 px down)
    r = ui.js("Aurora.V.pages[2].info.fields.find(f => f.n === 'samtykke').r")
    box = ui.page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]').bounding_box()
    (x0, y0), (x1, y1) = ui.at(2, r[0], r[1]), ui.at(2, r[2], r[3])
    assert max(abs(box["x"] - x0), abs(box["y"] - y0), abs(box["x"] + box["width"] - x1), abs(box["y"] + box["height"] - y1)) < 1.5


def _draw_signature(ui):
    ui.page.wait_for_selector(".scrim canvas.pad")
    box = ui.page.locator(".scrim canvas.pad").bounding_box()
    m = ui.page.mouse
    m.move(box["x"] + 60, box["y"] + 120)
    m.down()
    for k in range(1, 30):
        m.move(box["x"] + 60 + k * 12, box["y"] + 120 - 30 * ((k % 6) / 6), steps=2)
    m.up()


def test_handwritten_signature_is_saved_and_placed(ui):
    ui.open("plain")
    ui.panel("fillsign")
    ui.page.click("#sigNew")
    _draw_signature(ui)
    ui.modal_button(ui.t("saveSignature"))
    ui.page.wait_for_function("Aurora.S.mode === 'place'", timeout=10000)
    ui.click_at(0, 200, 400)
    ui.idle()
    stamps = [a for a in ui.annots(0) if a["t"] == "Stamp"]
    assert len(stamps) == 1
    assert ui.page.locator("#sigGrid .sigtile").count() == 1
    # resize keeps the proportions
    r0 = stamps[0]["r"]
    ui.tool("select")
    ui.click_at(0, (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2)
    ui.page.wait_for_selector(".ov .handle")
    hx, hy = r0[2] + 3, r0[3] + 3
    ui.drag(0, [(hx, hy), (hx + 60, hy + 5)], steps=8)
    ui.idle()
    r1 = [a for a in ui.annots(0) if a["t"] == "Stamp"][0]["r"]
    assert (r1[2] - r1[0]) == pytest.approx((r0[2] - r0[0]) + 60, abs=4)
    assert (r1[3] - r1[1]) / (r1[2] - r1[0]) == pytest.approx((r0[3] - r0[1]) / (r0[2] - r0[0]), rel=0.03)


def test_typed_signature(ui):
    ui.open("plain")
    ui.panel("fillsign")
    ui.page.click("#sigNew")
    ui.page.click('#sgTabs [data-v="type"]')
    ui.page.fill("#sgName", "Marko Petrović")
    ui.page.click('.fontpick [data-f="1"]')
    ui.modal_button(ui.t("saveSignature"))
    ui.page.wait_for_function("Aurora.S.mode === 'place'", timeout=10000)
    ui.page.keyboard.press("Escape")
    assert ui.js("Aurora.S.mode") == "select"
    items = ui.rpc("sig_list")["items"]
    assert len(items) >= 1


def test_digital_id_and_certificate_signature(ui):
    path = ui.open("report")
    ui.panel("fillsign")
    ui.page.click("#idNew")
    ui.page.fill("#idN", "Marko Petrović")
    ui.page.fill("#idE", "marko@example.com")
    ui.page.fill("#idP", "tajna123")
    ui.page.fill("#idP2", "tajna123")
    ui.modal_button(ui.t("create"))
    ui.page.wait_for_selector("#idMini .idrow")
    # sign into the form's empty signature field
    ui.show_page(2)
    out = ui.tmp / "report-signed.pdf"
    ui.dialogs(out)
    ui.page.click('.pg[data-i="2"] .fx .sigfield')
    ui.page.wait_for_selector("#dsPw")
    ui.page.fill("#dsPw", "tajna123")
    ui.page.fill("#dsR", "Godkjent")
    ui.modal_button(ui.t("continue"))
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=str(out), timeout=30000)
    ui.idle()
    d = ui.doc()
    assert d["signed"] and os.path.exists(out) and os.path.getsize(path) > 0
    ui.panel("fillsign") if ui.js("Aurora.S.panel") != "fillsign" else None
    ui.page.wait_for_selector(".sigstat", timeout=20000)
    assert ui.page.locator(".sigstat.untrusted, .sigstat.valid").count() == 1
    doc = pymupdf.open(out)
    sig = [w for w in doc[2].widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE])]
    assert [w.field_name for w in sig] == ["signatur"] and sig[0].is_signed
    # the signed field shows the signature, not the "sign here" button
    ui.show_page(2)
    assert ui.page.locator('.pg[data-i="2"] .fx .sigfield').count() == 0
    # a protected copy cannot keep a valid signature, so protecting a signed file asks first
    ui.panel("protect")
    ui.page.fill("#userPw", "otvori123")
    ui.dialogs(ui.tmp / "never-protected.pdf")
    ui.page.click("#doLock")
    ui.page.wait_for_selector(".scrim:not(.out) .m-foot")
    assert ui.t("signedCopyMsg") in ui.page.locator(".scrim:not(.out)").inner_text()
    ui.modal_button(ui.t("cancel"))
    assert not os.path.exists(ui.tmp / "never-protected.pdf")


def test_wrong_id_password_is_reported(ui):
    ui.open("plain")
    ui.rpc("id_create", {"name": "Test", "password": "abcd1234"})
    ui.panel("fillsign")
    ui.page.click("#dsGo")
    ui.page.wait_for_selector("#dsPw")
    ui.page.fill("#dsPw", "wrong")
    ui.page.uncheck("#dsVis", force=True)
    ui.dialogs(ui.tmp / "never.pdf")
    ui.modal_button(ui.t("continue"))
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.toast .msg')].some(e => e.textContent === t)",
                              arg=ui.t("idWrongPassword"), timeout=15000)
    assert not os.path.exists(ui.tmp / "never.pdf")


# ---------------------------------------------------------------- protect, watermark, redact

def test_protect_with_password_and_reopen(ui):
    ui.open("plain")
    ui.panel("protect")
    ui.page.fill("#userPw", "otvori123")
    ui.page.fill("#ownerPw", "vlasnik123")
    ui.page.uncheck("#p_copy", force=True)
    out = ui.tmp / "locked.pdf"
    ui.dialogs(out)
    ui.page.click("#doLock")
    ui.page.wait_for_function("document.querySelectorAll('.toast').length > 0", timeout=15000)
    d = pymupdf.open(out)
    assert d.needs_pass
    assert d.authenticate("otvori123") == 2
    # opening it asks for the password
    ui.js("(p) => Aurora.openPath(p, {skipGuard: true})", str(out))
    ui.page.wait_for_selector("#pwIn")
    ui.page.fill(".scrim:not(.out) #pwIn", "pogresna")
    ui.modal_button(ui.t("unlock"))
    ui.page.wait_for_selector(".scrim:not(.out) .hint.warn")
    ui.page.fill(".scrim:not(.out) #pwIn", "otvori123")
    ui.modal_button(ui.t("unlock"))
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=str(out), timeout=15000)
    d = ui.doc()
    assert d["protected"] and not d["owner"] and d["rights"]["copy"] is False
    assert ui.page.locator("#fileInfo .badge.warn").count() == 1


def test_watermark_and_header_footer(ui):
    ui.open("plain")
    revs = [p[2] for p in ui.doc()["pages"]]
    ui.panel("watermark")
    ui.page.fill("#wmt", "NACRT")
    ui.page.click("#wmApply")
    ui.idle()
    d = ui.doc()
    assert d["undo"] == "watermark" and all(p[2] != r for p, r in zip(d["pages"], revs))
    ui.page.click('#wmTabs [data-v="hf"]')
    ui.page.click('#hfPos [data-p="br"]')
    ui.page.click("#hfApply")
    ui.idle()
    assert ui.doc()["undo"] == "header"
    text = ui.rpc("page_text", {"page": 1})["text"]
    assert "NACRT" in text
    assert re.search(r"(Strana|Page|Side) 2 (od|of|av) 3", text)


def test_redact_area_and_search_then_apply(ui):
    ui.open("plain")
    ui.panel("redact")
    ui.page.fill("#rQ", "lorem")
    ui.page.click("#rFind")
    ui.idle()
    assert ui.page.inner_text("#rCount").endswith("3")
    ui.page.click("#rArea")
    ui.drag(0, [(48, 66), (300, 86)])
    ui.idle()
    ui.page.keyboard.press("Escape")
    assert ui.page.inner_text("#rCount").endswith("4")
    ui.page.click("#rApply")
    ui.modal_button(ui.t("redactApplyOk"))
    ui.idle()
    for i in range(3):
        text = ui.rpc("page_text", {"page": i})["text"]
        assert "lorem" not in text
    assert "Aurora sample" not in ui.rpc("page_text", {"page": 0})["text"]
    assert "Aurora sample page 2" in ui.rpc("page_text", {"page": 1})["text"]


# ---------------------------------------------------------------- pages

def test_organize_reorder_rotate_delete(ui):
    ui.open("plain")
    ui.panel("organize")
    ui.page.wait_for_selector("#grid .tile[data-i]")
    tiles = ui.page.locator("#grid .tile[data-i] .tframe")
    first, third = tiles.nth(0).bounding_box(), tiles.nth(2).bounding_box()
    m = ui.page.mouse
    m.move(first["x"] + 40, first["y"] + 40)
    m.down()
    m.move(third["x"] + third["width"] - 5, third["y"] + 40, steps=12)
    m.up()
    ui.idle()
    assert ui.doc()["undo"] == "reorder"
    assert "Aurora sample page 1" in ui.rpc("page_text", {"page": 2})["text"]
    # rotate the selected page from the panel
    ui.page.click("#oR")
    ui.idle()
    assert ui.doc()["undo"] == "rotate"
    w, h = ui.doc()["pages"][2][:2]
    assert w > h
    # delete with the keyboard: one confirm for one page is not needed
    ui.page.locator("#grid .tile[data-i]").nth(0).click()
    ui.page.keyboard.press("Delete")
    ui.idle()
    assert len(ui.doc()["pages"]) == 2
    # the panel stops naming the deleted page; it used to, while its buttons acted on another page
    assert ui.page.inner_text(".selinfo") == ui.t("noneSelected")
    # a selection made while organizing does not stay behind in the sidebar
    ui.page.locator("#grid .tile[data-i]").nth(0).click()
    ui.page.locator("#grid .tile[data-i]").nth(1).click(modifiers=["Control"])
    ui.page.click("#oDone")
    ui.settle_layout()
    assert ui.page.locator("#thumbs .tile.selected").count() == 0


def test_thumbnail_context_menu_duplicate_and_insert_blank(ui):
    ui.open("plain")
    ui.page.locator("#thumbs .tile").nth(1).click(button="right")
    ui.page.locator("#menu .m-item", has_text=ui.t("duplicate")).click()
    ui.idle()
    assert len(ui.doc()["pages"]) == 4
    ui.page.locator("#thumbs .tile").nth(0).click(button="right")
    ui.page.locator("#menu .m-item", has_text=ui.t("insertBlankAfter")).click()
    ui.idle()
    assert len(ui.doc()["pages"]) == 5
    assert ui.rpc("page_text", {"page": 1})["text"].strip() == ""


def test_bookmarks_and_attachments_sidebar(ui):
    ui.open("report")
    ui.page.click('#sideTabs [data-side="outline"]')
    ui.page.wait_for_selector("#outline .bm")
    titles = ui.js("[...document.querySelectorAll('#outline .bm-go')].map(e => e.textContent)")
    assert titles[:3] == ["Sammendrag", "Nøkkeltall", "Marked og kunder"]
    ui.page.locator("#outline .bm-go", has_text="Risiko").click()
    ui.page.wait_for_timeout(600)
    assert ui.js("Aurora.S.current") == 1
    ui.page.click("#sideAdd")
    ui.page.fill("#pIn", "Moj obeleživač")
    ui.modal_button(ui.t("ok"))
    ui.idle()
    assert "Moj obeleživač" in ui.js("[...document.querySelectorAll('#outline .bm-go')].map(e => e.textContent)")
    ui.page.click('#sideTabs [data-side="attach"]')
    ui.page.wait_for_selector("#attachList .att")
    assert ui.page.inner_text("#attachList .att-n") == "nokkeltall.csv"


def test_crop_one_page(ui):
    ui.open("plain")
    ui.panel("organize")
    ui.page.click("#oCrop")
    ui.page.wait_for_function("Aurora.S.mode === 'crop'")
    ui.settle_layout()
    ui.drag(0, [(40, 50), (380, 300)])
    ui.page.wait_for_selector(".scrim")
    ui.modal_button(ui.t("cropThis"))
    ui.idle()
    w, h = ui.doc()["pages"][0][:2]
    assert w == pytest.approx(340, abs=2) and h == pytest.approx(250, abs=2)
    assert ui.doc()["pages"][1][:2] == [420, 595]


# ---------------------------------------------------------------- OCR, export, combine

def test_ocr_scanned_page(ui):
    ui.open("report")
    assert ui.rpc("page_text", {"page": 3})["text"].strip() == ""
    ui.panel("ocr")
    ui.page.check("#ol_nor", force=True)
    ui.page.fill("#ocrPg", "4")
    ui.page.click("#ocrGo")
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.toast .msg')].some(e => e.textContent.startsWith(t))",
                              arg=ui.t("ocrDone").split(".")[0], timeout=60000)
    text = ui.rpc("page_text", {"page": 3})["text"]
    assert "Bekreftelse" in text and "kvartalsrapporten" in text
    assert ui.doc()["undo"] == "ocr"


@pytest.mark.parametrize("kind,check", [("txt", "Driftsinntektene"), ("docx", None)])
def test_export(ui, kind, check):
    ui.open("report")
    out = ui.tmp / f"export.{kind}"
    ui.panel("export")
    ui.dialogs(out)
    ui.page.click(f'.excard[data-k="{kind}"]')
    ui.page.wait_for_function("document.querySelectorAll('.toast').length > 0", timeout=120000)
    assert out.exists() and out.stat().st_size > 0
    if check:
        assert check in out.read_text(encoding="utf-8")


def test_export_page_images(ui):
    ui.open("plain")
    folder = ui.tmp / "imgs"
    folder.mkdir()
    ui.panel("export")
    ui.page.click('#exF [data-v="jpg"]')
    ui.dialogs(folder)
    ui.page.click('.excard[data-k="images"]')
    ui.page.wait_for_function("document.querySelectorAll('.toast').length > 0", timeout=60000)
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == 3 and all(f.endswith(".jpg") for f in files)


def test_combine_files(ui, docs):
    ui.panel("combine")
    ui.dialogs([docs["plain"], docs["report"]])
    ui.page.click("#mPick")
    ui.page.wait_for_function("document.querySelectorAll('#mList .mrow').length === 2")
    ui.page.locator("#mList .mrow").nth(1).locator('[data-a="up"]').click()
    out = ui.tmp / "merged.pdf"
    ui.dialogs(out)
    ui.page.click("#mGo")
    ui.page.wait_for_function("document.querySelectorAll('.toast').length > 0", timeout=20000)
    d = pymupdf.open(out)
    assert len(d) == 7
    assert "Tredje kvartal" in d[0].get_text()


# ---------------------------------------------------------------- saving, printing, guarding

def test_save_and_unsaved_changes_guard(ui):
    path = ui.open("plain")
    ui.tool("rect")
    ui.drag(0, [(100, 200), (200, 260)])
    ui.idle()
    assert ui.doc()["dirty"] and ui.page.locator("#fileInfo .dirty.on").count() == 1
    # opening another file asks first; cancel keeps everything
    ui.js("(p) => { Aurora.openPath(p); }", ui.docs["report"])  # don't wait: it asks first
    ui.page.wait_for_selector(".scrim")
    ui.modal_button(ui.t("cancel"))
    assert ui.doc()["path"] == path
    ui.page.keyboard.press("Control+s")
    ui.page.wait_for_function("!Aurora.S.doc.dirty", timeout=15000)
    assert [a.type[1] for a in pymupdf.open(path)[0].annots()] == ["Square"]



def test_closing_the_window_commits_typed_text_first(ui):
    ui.open("report")
    ui.show_page(2)
    ui.page.locator('.pg[data-i="2"] .fx input[title="navn"]').click()
    ui.page.keyboard.type("Kari")  # still in the field: nothing has reached the document yet
    assert not ui.doc()["dirty"]
    ui.js("() => { Aurora.confirmClose(); }")  # what Python calls when the window's X is clicked
    ui.page.wait_for_selector(".scrim:not(.out) .m-foot")
    assert ui.doc()["dirty"]
    ui.modal_button(ui.t("cancel"))


def test_closing_without_changes_quits_at_once(ui):
    ui.open("plain")
    ui.js("""() => { window.__quit = 0; const rpc = Aurora.rpc;
      Aurora.rpc = (m, p) => m === 'quit' ? (window.__quit++, Promise.resolve({ok: true})) : rpc(m, p);
      Aurora.confirmClose(); }""")
    ui.page.wait_for_function("window.__quit === 1", timeout=10000)
    assert ui.page.locator(".scrim:not(.out)").count() == 0

def test_save_as_and_recent_files(ui):
    ui.open("plain")
    out = ui.tmp / "copy.pdf"
    ui.dialogs(out)
    ui.page.keyboard.press("Control+Shift+S")
    ui.page.wait_for_function("(p) => Aurora.S.doc.path === p", arg=str(out), timeout=15000)
    ui.page.click("#menuBtn")
    ui.page.locator("#menu .m-item", has_text=ui.t("closeDoc")).click()
    ui.page.wait_for_selector("#start .ritem")
    assert ui.page.locator("#start .ritem .rn").first.inner_text() == "copy.pdf"


def test_print_renders_every_page_into_the_print_frame(ui):
    ui.open("report")
    ui.js("""() => { window.__printed = 0; const f = document.querySelector('#printFrame');
      f.addEventListener('load', () => { f.contentWindow.print = () => { window.__printed++; }; }); }""")
    ui.page.keyboard.press("Control+p")
    ui.page.wait_for_selector(".scrim #prSeg")
    ui.modal_button(ui.t("printGo"))
    ui.page.wait_for_function("window.__printed === 1", timeout=20000)
    n = ui.js("document.querySelector('#printFrame').contentDocument.querySelectorAll('img').length")
    assert n == 4


# ---------------------------------------------------------------- languages and misc

def test_languages_switch_everything(ui):
    ui.open("plain")
    for lang, save in (("en", "Save"), ("no", "Lagre"), ("sr", "Sačuvaj")):
        ui.js("Aurora.openPanel('settings')")
        ui.page.click(f'#sLang [data-v="{lang}"]')
        ui.page.wait_for_timeout(200)
        assert ui.page.inner_text("#saveBtn").strip() == save
        # nothing on screen is an untranslated key
        raw = ui.js(r"""() => [...document.querySelectorAll('body *')].filter(e => e.children.length === 0)
            .map(e => e.textContent.trim()).filter(t => /^(e|t|op|an|mode|sig|st|p)_[A-Za-z]+$|^[a-z]+[A-Z][A-Za-z]+$/.test(t))""")
        assert raw == [], raw


def test_theme_follows_windows_and_the_choice_is_remembered(ui):
    page = ui.page
    theme = lambda: ui.js("document.documentElement.dataset.theme")  # noqa: E731
    assert theme() == "dark"
    page.emulate_media(color_scheme="light")  # Windows switches to light apps
    page.wait_for_function("document.documentElement.dataset.theme === 'light'", timeout=5000)
    assert ui.js("getComputedStyle(document.body).backgroundColor") == "rgb(243, 246, 250)"
    # the switch in the menu is an explicit choice: it wins over Windows and survives a restart
    page.click("#menuBtn")
    page.locator("#menu .m-item", has_text=ui.t("lightTheme")).click()
    assert theme() == "dark"
    page.wait_for_function("async () => (await Aurora.rpc('state')).settings.theme === 'dark'", polling=100, timeout=5000)
    page.reload()
    page.wait_for_selector("body.ready", timeout=20000)
    assert theme() == "dark"
    ui.js("Aurora.openPanel('settings')")
    page.click('#sTheme [data-v="system"]')
    assert theme() == "light"


def test_night_mode_and_keyboard_tools(ui):
    ui.open("plain")
    ui.page.click('#vbar [data-v="night"]')
    assert ui.js("document.querySelector('#app').classList.contains('night')")
    ui.page.mouse.click(700, 500)
    ui.page.keyboard.press("p")
    assert ui.js("Aurora.S.mode") == "pen" and ui.js("Aurora.S.panel") == "annotate"
    ui.settle_layout()
    ui.page.keyboard.press("Escape")
    assert ui.js("Aurora.S.mode") == "select"
    ui.page.keyboard.press("PageDown")
    ui.page.wait_for_function("Aurora.S.current === 1", timeout=5000)
