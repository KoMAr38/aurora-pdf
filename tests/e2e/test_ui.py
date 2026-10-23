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
from core import pdftools as pt  # noqa: E402

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
    try:  # the next test starts as the first did: no document, no saved signatures or Digital IDs
        rpc(page, "test_dialogs", {"answers": []})
        rpc(page, "close_doc")
        rpc(page, "settings_set", {"lang": "sr", "theme": "system", "night": False, "fit": "width", "ocr_langs": "",
                                   "sig_reason": "", "sig_location": ""})
        for item in rpc(page, "sig_list")["items"]:
            rpc(page, "sig_remove", {"id": item["id"]})  # moved aside in the test's data folder
        for item in rpc(page, "ids_list")["items"]:
            rpc(page, "id_remove", {"id": item["id"]})
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
    ui.page.wait_for_function("() => Aurora.V.pages[200].mat && Aurora.V.pages[200].info", timeout=20000)
    assert ui.js("Aurora.S.current") == 200
    assert ui.page.input_value("#pageIn") == "201"
    alive = ui.js("Aurora.V.pages.map((p, i) => p.mat ? i : -1).filter(i => i >= 0)")
    assert len(alive) <= 12 and all(185 <= i <= 215 for i in alive)
    # sharp image for the visible page arrives at device width
    ui.page.wait_for_function("() => Aurora.V.pages[200].hi.classList.contains('ready')", timeout=20000)


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


def test_space_pans_without_scrolling_the_view(ui, tmp_path):
    ui.open(samples.build_big(tmp_path / "big.pdf", pages=20), copy=False)
    ui.click_at(0, 300, 400)  # on the page, as before any pan
    top = ui.js("document.querySelector('#viewer').scrollTop")
    ui.page.keyboard.down("Space")
    ui.page.wait_for_timeout(600)  # held: the key repeats
    assert ui.js("document.querySelector('#pages').classList.contains('handmode')")
    ui.page.keyboard.up("Space")
    ui.page.keyboard.press("Space")  # a tap
    ui.page.wait_for_timeout(300)
    assert ui.js("document.querySelector('#viewer').scrollTop") == top


def test_search_highlights_and_steps_through_hits(ui):
    ui.open("report")
    ui.page.click("#search")
    ui.page.keyboard.type("prosent")
    ui.page.wait_for_function("() => Aurora.S.search.hits.length > 0", timeout=10000)
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


def test_link_leads_to_its_page_after_pages_are_inserted(ui, tmp_path):
    doc = pymupdf.open(ui.docs["plain"])
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(48, 60, 260, 86), "page": 2})
    path = tmp_path / "links.pdf"
    doc.save(str(path))
    ui.open(str(path), copy=False)
    ui.js("Aurora.edit('insert_blank', {at: 1})")
    ui.idle()
    assert len(ui.doc()["pages"]) == 4
    ui.show_page(0)
    ui.page.locator('.pg[data-i="0"] .lk a.link').click()
    ui.page.wait_for_function("() => Aurora.S.current === 3", timeout=5000)  # page 3 of the file is the 4th now


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
    ui.page.wait_for_function("() => document.activeElement?.matches('.ft-edit')", timeout=5000)
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
    ui.page.wait_for_function("() => document.querySelectorAll('#clist .citem').length === 2", timeout=10000)


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


def test_undo_right_after_a_pen_stroke_takes_back_the_stroke(ui):
    """Strokes wait 0.9 s to go out together; Ctrl+Z in that time used to take back the edit before them."""
    ui.open("plain")
    ui.tool("rect")
    ui.drag(0, [(100, 200), (200, 260)])
    ui.idle()
    ui.tool("pen")
    ui.drag(0, [(80, 330), (140, 360), (220, 340)])
    ui.page.keyboard.press("Control+z")  # at once, while the stroke still waits
    ui.idle(1200)
    assert [a["t"] for a in ui.annots(0)] == ["Square"]
    ui.page.keyboard.press("Control+y")
    ui.idle()
    assert sorted(a["t"] for a in ui.annots(0)) == ["Ink", "Square"]
    # the stroke's page image and text were on their way when undo dropped its revision: those answer 404
    ui.errors[:] = [e for e in ui.errors if not re.fullmatch(r"HTTP 404 \S+/(page/\d+/\d+\.jpg|info/\d+\.json)", e)]


def test_a_stroke_stays_on_its_page_when_an_earlier_page_goes(ui):
    ui.open("plain")
    ui.tool("pen")
    ui.show_page(2)
    ui.drag(2, [(80, 330), (140, 360), (220, 340)])
    ui.page.locator("#thumbs .tile").nth(0).click(button="right")  # while the stroke still waits
    ui.page.locator("#menu .m-item", has_text=ui.t("deletePage")).click()
    ui.idle(1200)
    assert [(a["t"], a["p"]) for a in ui.annots()] == [("Ink", 1)]
    assert "Aurora sample page 3" in ui.rpc("page_text", {"page": 1})["text"]
    assert ui.page.locator(".ov .pre-ink").count() == 0


def test_a_shape_finished_while_a_page_delete_is_on_its_way_stays_on_its_page(ui):
    ui.open("plain")
    ui.tool("rect")
    ui.show_page(2)
    (x0, y0), (x1, y1) = ui.at(2, 100, 200), ui.at(2, 200, 260)
    ui.js("""([x0, y0, x1, y1]) => {
      Aurora.edit('delete_pages', {pages: [0]});  // on its way: the busy veil shows only after 280 ms
      const ev = (type, x, y) => new PointerEvent(type, {bubbles: true, clientX: x, clientY: y, pointerId: 1,
                                                         pointerType: 'mouse', button: 0, buttons: 1});
      document.elementFromPoint(x0, y0).dispatchEvent(ev('pointerdown', x0, y0));
      window.dispatchEvent(ev('pointermove', x1, y1));
      window.dispatchEvent(ev('pointerup', x1, y1)); }""", [x0, y0, x1, y1])
    ui.idle(800)
    assert [(a["t"], a["p"]) for a in ui.annots()] == [("Square", 1)]
    assert "Aurora sample page 3" in ui.rpc("page_text", {"page": 1})["text"]


def test_eraser_removes_clicked_annotation(ui):
    ui.open("plain")
    ui.tool("ellipse")
    ui.drag(0, [(100, 300), (220, 380)])
    ui.idle()
    ui.tool("eraser")
    ui.click_at(0, 160, 340)
    ui.idle()
    assert ui.annots(0) == []


def test_a_selection_does_not_outlive_its_comment_or_its_document(ui, tmp_path):
    """Object numbers repeat between files: a selection left over picked, and Delete removed, a comment of the next file."""
    for name, rect in (("a.pdf", (100, 200, 200, 260)), ("b.pdf", (220, 300, 330, 380))):
        d = pymupdf.open(ui.docs["plain"])
        d[0].add_rect_annot(pymupdf.Rect(rect))
        d.save(str(tmp_path / name))
    ui.open(str(tmp_path / "a.pdf"), copy=False)
    ui.tool("select")
    ui.click_at(0, 150, 201)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.open(str(tmp_path / "b.pdf"), copy=False)
    assert ui.js("Aurora.S.selAnnot") is None
    ui.page.keyboard.press("Delete")
    ui.idle()
    assert len(ui.annots(0)) == 1 and not ui.doc()["dirty"]
    # undo takes the selected comment away, and the selection with it
    ui.tool("rect")
    ui.drag(0, [(60, 330), (160, 380)])
    ui.idle()
    ui.tool("select")
    ui.click_at(0, 110, 331)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.page.keyboard.press("Control+z")
    ui.idle()
    assert ui.js("Aurora.S.selAnnot") is None and ui.page.locator("#annbar.hidden").count() == 1


def test_the_comments_list_keeps_its_place_when_one_goes(ui, tmp_path):
    ui.open(samples.build_big(tmp_path / "big.pdf", pages=60), copy=False)
    for i in range(60):
        ui.js("(i) => Aurora.edit('annot_add', {page: i, kind: 'note', point: [100, 100], text: 'Komentar ' + i})", i)
    ui.idle()
    ui.panel("annotate")
    ui.page.wait_for_function("() => document.querySelectorAll('#clist .citem').length === 60", timeout=20000)
    ui.js("() => { const p = document.querySelector('#panel'); p.scrollTop = p.scrollHeight; }")
    ui.page.wait_for_timeout(200)
    top = ui.js("document.querySelector('#panel').scrollTop")
    assert top > 1000
    ui.js("document.querySelectorAll('#clist .citem')[55].querySelector('[data-del]').click()")
    ui.page.wait_for_function("() => document.querySelectorAll('#clist .citem').length === 59", timeout=20000)
    assert ui.js("document.querySelector('#panel').scrollTop") > top - 150  # one row less, not back at the top


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


def test_a_click_on_a_check_box_counts_right_after_typing_or_choosing(ui):
    """The layer waits to be rebuilt while a field holds the keyboard. Pressing on the check box took the
    keyboard away and rebuilt the layer before the button came up, so the box under the pointer was new
    and the click was lost."""
    ui.open("report")
    ui.show_page(2)
    page = ui.page
    chk = page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]')

    def click_like_a_hand():
        b = chk.bounding_box()
        page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(90)
        page.mouse.up()
        ui.idle()

    page.locator('.pg[data-i="2"] .fx input[title="navn"]').click()
    page.keyboard.type("Kari")  # typed, then straight to the box
    click_like_a_hand()
    assert "on" in chk.get_attribute("class").split()
    sel = page.locator('.pg[data-i="2"] .fx select[title="avdeling"]')
    sel.focus()
    sel.select_option("Økonomi")  # the list keeps the keyboard after the choice
    ui.idle()
    click_like_a_hand()
    assert "on" not in chk.get_attribute("class").split()
    vals = ui.js("Aurora.V.pages[2].info.fields.reduce((o, f) => (o[f.n] = f.v, o), {})")
    assert vals["navn"] == "Kari" and vals["avdeling"] == "Økonomi" and not vals["samtykke"]


def test_dropdown_shows_labels_and_stores_values(ui, tmp_path):
    doc = pymupdf.open(ui.docs["plain"])
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "land", pymupdf.Rect(50, 300, 250, 322)
    w.choice_values = [["RS", "Serbia"], ["NO", "Norway"], ["SE", "Sweden"]]
    w.field_value = "NO"
    doc[0].add_widget(w)
    path = tmp_path / "land.pdf"
    doc.save(str(path))
    ui.open(str(path), copy=False)
    sel = ui.page.locator('.pg[data-i="0"] .fx select[title="land"]')
    assert sel.evaluate("s => s.selectedOptions[0].textContent") == "Norway"
    sel.select_option(label="Sweden")
    ui.idle()
    assert ui.js("Aurora.V.pages[0].info.fields.find(f => f.n === 'land').v") == "SE"


def test_list_takes_several_choices(ui, tmp_path):
    doc = pymupdf.open(ui.docs["plain"])
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_LISTBOX, "jezici", pymupdf.Rect(50, 300, 250, 360)
    w.choice_values = [["sr", "Srpski"], ["no", "Norsk"], ["en", "English"]]
    w.field_value, w.field_flags = "no", 1 << 21  # MultiSelect
    doc[0].add_widget(w)
    path = tmp_path / "jezici.pdf"
    doc.save(str(path))
    ui.open(str(path), copy=False)
    sel = ui.page.locator('.pg[data-i="0"] .fx select[title="jezici"]')
    assert sel.evaluate("s => s.multiple && [...s.selectedOptions].map(o => o.value)") == ["no"]
    sel.select_option(["sr", "en"])
    ui.idle()
    assert ui.js("Aurora.V.pages[0].info.fields.find(f => f.n === 'jezici').v") == ["sr", "en"]
    assert ui.toasts() == []


def test_an_empty_dropdown_shows_no_choice_and_takes_the_first(ui, tmp_path):
    """A dropdown with no value showed its first option, which the file did not hold, and picking it sent nothing."""
    doc = pymupdf.open(ui.docs["plain"])
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "grad", pymupdf.Rect(50, 300, 250, 322)
    w.choice_values, w.field_value = ["Beograd", "Niš", "Oslo"], ""  # nothing chosen yet, as in a blank form
    doc[0].add_widget(w)
    path = tmp_path / "grad.pdf"
    doc.save(str(path))
    ui.open(str(path), copy=False)
    sel = ui.page.locator('.pg[data-i="0"] .fx select[title="grad"]')
    assert sel.evaluate("s => s.selectedIndex") == -1
    sel.focus()
    ui.page.keyboard.press("ArrowDown")  # the first choice; it used to be passed over for the second
    ui.page.wait_for_timeout(300)
    ui.idle()
    assert ui.js("Aurora.V.pages[0].info.fields.find(f => f.n === 'grad').v") == "Beograd"


def test_a_box_ticked_with_the_keyboard_keeps_the_focus(ui):
    """The field layer is drawn again when the answer comes; Tab and Space went on from the top of the page."""
    ui.open("report")
    ui.show_page(2)
    ui.page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]').focus()
    for ticked in (True, False):
        ui.page.keyboard.press("Space")
        ui.page.wait_for_function("(v) => !!Aurora.V.pages[2].info.fields.find(f => f.n === 'samtykke').v === v", arg=ticked)
        ui.page.wait_for_timeout(300)
        assert ui.js("document.activeElement.title") == "samtykke"
        assert ("on" in ui.page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]').get_attribute("class").split()) is ticked


def test_a_ticked_box_shows_the_file_again_after_undo(ui):
    ui.open("report")
    ui.show_page(2)
    chk = ui.page.locator('.pg[data-i="2"] .fx .chk[title="samtykke"]')
    chk.click()
    ui.idle()
    assert "on" in chk.get_attribute("class").split()
    ui.page.keyboard.press("Control+z")
    ui.idle()
    ui.click_at(2, 400, 700)  # elsewhere on the page
    ui.idle()
    assert "on" not in chk.get_attribute("class").split()


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
    ui.page.wait_for_function("() => Aurora.S.mode === 'place'", timeout=10000)
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
    ui.page.wait_for_function("() => Aurora.S.mode === 'place'", timeout=10000)
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


def test_a_dialogs_own_check_shows_its_toast_on_top(ui):
    """The dialog stays open and answers with a toast, which used to land under its dimmed backdrop."""
    ui.open("plain")
    ui.panel("fillsign")
    ui.page.click("#idNew")
    ui.page.fill("#idN", "")  # it starts with the author's name from the settings
    ui.modal_button(ui.t("create"))
    toast = ui.page.locator(".toast", has_text=ui.t("idNeedName"))
    toast.wait_for()
    b = toast.bounding_box()
    assert ui.js("([x, y]) => !!document.elementFromPoint(x, y).closest('.toast')", [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2])
    ui.modal_button(ui.t("cancel"))


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
    ui.page.wait_for_function("() => document.querySelectorAll('.toast').length > 0", timeout=15000)
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
    assert "NACRT" not in text  # the watermark is drawn, so it cannot be selected or copied
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


def test_shift_click_starts_afresh_after_its_page_went(ui):
    ui.open("plain")
    ui.panel("organize")
    ui.page.wait_for_selector("#grid .tile[data-i]")
    ui.page.locator("#grid .tile[data-i]").nth(2).click()
    ui.page.keyboard.press("Delete")
    ui.idle()
    ui.page.locator("#grid .tile[data-i]").nth(0).click(modifiers=["Shift"])
    assert ui.js("[...Aurora.S.selected]") == [0]  # not pages 1 to 3 of a file that has 2


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


def test_an_edit_redraws_only_the_thumbnail_of_its_page(ui, tmp_path):
    ui.open(samples.build_big(tmp_path / "long.pdf", pages=12), copy=False)
    tiles = "[...document.querySelectorAll('#thumbs .tile')]"
    ui.js(f"{tiles}.forEach(t => t.__kept = true)")
    before = ui.js(f"{tiles}.map(t => t.querySelector('img').src)")
    ui.js("Aurora.edit('annot_add', {page: 3, kind: 'note', point: [100, 100], text: 'x'})")
    ui.idle()
    assert ui.js(f"{tiles}.every(t => t.__kept)")  # one note used to rebuild all of them
    after = ui.js(f"{tiles}.map(t => t.querySelector('img').src)")
    assert [k for k, (a, b) in enumerate(zip(before, after)) if a != b] == [3]
    ui.js("Aurora.edit('insert_blank', {at: 0})")
    ui.idle()
    assert ui.js(f"{tiles}.map(t => t.querySelector('.tnum').textContent)") == [str(n) for n in range(1, 14)]
    assert ui.js(f"{tiles}.map(t => t.querySelector('img').src)")[1:] == after


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
    ui.page.wait_for_function("() => Aurora.S.mode === 'crop'")
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
    ui.page.wait_for_timeout(300)
    assert ui.page.is_checked("#ol_nor")  # the panel, drawn again after the run, still shows the languages used


def test_a_job_closes_its_own_progress_dialog(ui):
    """A dialog opened over the progress (the window's X, a dropped file) stays open; the progress goes."""
    ui.open("report")
    ui.panel("ocr")
    ui.page.fill("#ocrPg", "4")
    ui.page.click("#ocrGo")
    ui.page.wait_for_selector(".scrim:not(.out) .job")
    ui.js("() => { Aurora.modal({ title: 'Over it', body: '<p>x</p>' }); }")
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.toast .msg')].some(e => e.textContent.startsWith(t))",
                              arg=ui.t("ocrDone").split(".")[0], timeout=60000)
    ui.page.wait_for_timeout(400)
    assert ui.js("[...document.querySelectorAll('.scrim:not(.out) h3')].map(h => h.textContent)") == ["Over it"]
    ui.modal_button(ui.t("ok"))


@pytest.mark.parametrize("kind,check", [("txt", "Driftsinntektene"), ("docx", None)])
def test_export(ui, kind, check):
    ui.open("report")
    out = ui.tmp / f"export.{kind}"
    ui.panel("export")
    ui.dialogs(out)
    ui.page.click(f'.excard[data-k="{kind}"]')
    ui.page.wait_for_function("() => document.querySelectorAll('.toast').length > 0", timeout=120000)
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
    ui.page.wait_for_function("() => document.querySelectorAll('.toast').length > 0", timeout=60000)
    files = sorted(p.name for p in folder.iterdir())
    assert len(files) == 3 and all(f.endswith(".jpg") for f in files)


def test_combine_files(ui, docs):
    ui.panel("combine")
    ui.dialogs([docs["plain"], docs["report"]])
    ui.page.click("#mPick")
    ui.page.wait_for_function("() => document.querySelectorAll('#mList .mrow').length === 2")
    ui.page.locator("#mList .mrow").nth(1).locator('[data-a="up"]').click()
    out = ui.tmp / "merged.pdf"
    ui.dialogs(out)
    ui.page.click("#mGo")
    ui.page.wait_for_function("() => document.querySelectorAll('.toast').length > 0", timeout=20000)
    d = pymupdf.open(out)
    assert len(d) == 7
    assert "Tredje kvartal" in d[0].get_text()


def test_combining_the_open_file_asks_to_save_it_first(ui, docs):
    """Its file does not hold the changes on screen yet, and they used to be left out without a word."""
    ui.open("plain")
    ui.js("Aurora.edit('rotate', {pages: [0], degrees: 90})")
    ui.idle()
    ui.panel("combine")
    ui.page.click("#mCur")
    ui.dialogs([docs["report"]])
    ui.page.click("#mPick")
    ui.page.wait_for_function("() => document.querySelectorAll('#mList .mrow').length === 2")
    ui.page.click("#mGo")
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.toast .msg')].some(e => e.textContent === t)",
                              arg=ui.t("e_SAVE_FIRST"), timeout=10000)


def test_protected_file_goes_in_with_its_owner_password(ui, docs):
    theirs = ui.tmp / "theirs.pdf"
    pt.save_pdf(pymupdf.open(docs["plain"]), str(theirs), owner_pw="boss", allowed=["print"])
    n, ours = len(pymupdf.open(theirs)), len(pymupdf.open(docs["report"]))
    ui.open("report")
    ui.panel("organize")
    ui.dialogs(theirs)
    ui.page.click("#oFile")
    ui.page.fill(".scrim:not(.out) #srcPw", "nope")
    ui.modal_button(ui.t("unlock"))
    ui.page.wait_for_selector(".scrim:not(.out) .hint.warn")  # wrong: asked again
    ui.page.fill(".scrim:not(.out) #srcPw", "boss")
    ui.modal_button(ui.t("unlock"))
    ui.page.wait_for_function("(n) => Aurora.S.doc.pages.length === n", arg=ours + n, timeout=20000)

    ui.panel("combine")
    ui.dialogs([docs["report"], str(theirs)])
    ui.page.click("#mPick")
    ui.page.wait_for_function("() => document.querySelectorAll('#mList .mrow').length === 2")
    out = ui.tmp / "merged.pdf"
    ui.dialogs(out)  # asked for after the password
    ui.page.click("#mGo")
    ui.page.fill(".scrim:not(.out) #srcPw", "boss")
    ui.modal_button(ui.t("unlock"))
    ui.page.wait_for_function("() => document.querySelectorAll('.toast.good').length > 0", timeout=20000)
    assert len(pymupdf.open(out)) == ours + n


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
    ui.page.wait_for_function("() => !Aurora.S.doc.dirty", timeout=15000)
    assert [a.type[1] for a in pymupdf.open(path)[0].annots()] == ["Square"]


def _press_and_wait_for_toast(ui, key):
    n = ui.js("document.querySelectorAll('.toast').length")
    ui.page.keyboard.press(key)
    ui.page.wait_for_function("(n) => document.querySelectorAll('.toast').length > n", arg=n, timeout=15000)
    ui.idle()


def test_ctrl_s_saves_what_is_still_being_typed(ui):
    """Ctrl+S right after typing, with the cursor still in the field or the text box."""
    path = ui.open("report")
    ui.show_page(2)
    ui.page.locator('.pg[data-i="2"] .fx input[title="navn"]').click()
    ui.page.keyboard.type("Kari")
    _press_and_wait_for_toast(ui, "Control+s")
    assert not ui.doc()["dirty"]
    assert {w.field_name: w.field_value for w in pymupdf.open(path)[2].widgets()}["navn"] == "Kari"
    ui.tool("text")
    ui.click_at(2, 300, 460)  # under the signature field
    ui.page.wait_for_function("() => document.activeElement?.matches('.ft-edit')", timeout=5000)
    ui.page.keyboard.type("Hilsen")
    _press_and_wait_for_toast(ui, "Control+s")
    assert not ui.doc()["dirty"]
    assert [a.info["content"] for a in pymupdf.open(path)[2].annots()] == ["Hilsen"]


def test_save_without_a_file_name_goes_on_through_save_as(ui, tmp_path):
    """An image opened as a PDF has no file yet: "save, then open the other one" used to stop after saving."""
    from PIL import Image

    img = tmp_path / "scan.png"
    Image.new("RGB", (300, 400), "white").save(img)
    ui.js("(p) => Aurora.openPath(p, {skipGuard: true})", str(img))
    ui.page.wait_for_function("() => Aurora.S.doc && Aurora.S.doc.name === 'scan.pdf'", timeout=15000)
    ui.wait_pages()  # else a late request for the page before the rotation gets a 404, which fails the test
    ui.js("Aurora.edit('rotate', {pages: [0], degrees: 90})")
    ui.idle()
    out = ui.tmp / "scan.pdf"
    ui.dialogs(out)
    ui.js("(p) => { Aurora.openPath(p); }", ui.docs["plain"])  # asks about the unsaved changes first
    ui.page.wait_for_selector(".scrim:not(.out) .m-foot")
    ui.modal_button(ui.t("save"))
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=ui.docs["plain"], timeout=15000)
    assert pymupdf.open(out)[0].rotation == 90


def test_save_as_that_needs_the_open_password_still_saves_a_copy(ui):
    locked = ui.tmp / "policy.pdf"
    pt.save_pdf(pymupdf.open(ui.docs["plain"]), str(locked), user_pw="open1", owner_pw="owner1", allowed=["print"])
    ui.js("(p) => Aurora.call('open_path', {path: p, password: 'owner1'})", str(locked))  # the owner opens it
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=str(locked), timeout=15000)
    ui.wait_pages()
    ui.js("Aurora.edit('rotate', {pages: [0], degrees: 90})")
    ui.idle()
    out = ui.tmp / "policy-copy.pdf"
    ui.dialogs(out)
    ui.page.keyboard.press("Control+Shift+S")
    ui.page.wait_for_selector(".scrim:not(.out) #npU")
    ui.page.fill(".scrim:not(.out) #npU", "open1")
    ui.modal_button(ui.t("keepProtection"))
    ui.page.wait_for_function("(p) => Aurora.S.doc.path === p", arg=str(out), timeout=15000)
    copy, original = pymupdf.open(out), pymupdf.open(locked)
    assert copy.authenticate("open1") and copy[0].rotation == 90
    assert original.authenticate("open1") and original[0].rotation == 0  # it used to be saved over instead


def test_pages_taken_out_without_protection_leave_the_document_protected(ui):
    """The owner who lacks the open password may take pages out without protection. That answer used
    to drop the protection of the open document too, and its next save had no password."""
    locked = ui.tmp / "policy.pdf"
    pt.save_pdf(pymupdf.open(ui.docs["plain"]), str(locked), user_pw="open1", owner_pw="owner1", allowed=["print"])
    ui.js("(p) => Aurora.call('open_path', {path: p, password: 'owner1'})", str(locked))
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p", arg=str(locked), timeout=15000)
    ui.wait_pages()
    out = ui.tmp / "page-1.pdf"
    ui.dialogs(out)
    n = ui.js("document.querySelectorAll('.toast').length")
    ui.js("Aurora.extractPages([0])")
    ui.page.wait_for_selector(".scrim:not(.out) #npU")
    assert ui.js("document.querySelector('.scrim:not(.out) p').textContent") == ui.t("needPasswordsCopyMsg")
    ui.modal_button(ui.t("removeProtection"))
    ui.page.wait_for_function("(n) => document.querySelectorAll('.toast').length > n", arg=n, timeout=15000)
    assert not pymupdf.open(out).needs_pass and len(pymupdf.open(out)) == 1
    assert ui.js("[Aurora.S.doc.protected, Aurora.S.doc.dirty]") == [True, False]
    assert pymupdf.open(locked).needs_pass


def test_compressing_a_signed_document_asks_first(ui):
    ui.open("plain")
    ident = ui.rpc("id_create", {"name": "Ana Test", "password": "pw1234"})["id"]
    signed = ui.tmp / "signed.pdf"
    ui.dialogs(signed)
    ui.js("(id) => Aurora.call('sign_digital', {id, password: 'pw1234'})", ident)  # opens the signed file
    ui.page.wait_for_function("(p) => Aurora.S.doc && Aurora.S.doc.path === p && Aurora.S.doc.signed",
                              arg=str(signed), timeout=30000)
    ui.panel("info")
    ui.page.wait_for_selector("#cGo")
    ui.dialogs(ui.tmp / "small.pdf")
    ui.page.click("#cGo")
    ui.page.wait_for_selector(".scrim:not(.out) .m-foot")
    assert ui.t("signedCopyMsg") in ui.page.locator(".scrim:not(.out)").inner_text()
    ui.modal_button(ui.t("cancel"))
    ui.page.wait_for_timeout(300)
    assert not (ui.tmp / "small.pdf").exists()



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
    ui.page.wait_for_function("() => window.__quit === 1", timeout=10000)
    assert ui.page.locator(".scrim:not(.out)").count() == 0


def test_closing_while_a_job_runs_asks_and_stops_it_first(ui):
    ui.open("report")
    for _ in range(3):
        ui.js("Aurora.edit('duplicate', {pages: [3]})")  # four scanned pages: the job runs for seconds
    ui.idle()
    ui.panel("ocr")
    ui.page.fill("#ocrPg", "4-7")
    ui.page.click("#ocrGo")
    ui.page.wait_for_selector(".scrim:not(.out) .job")
    ui.js("""() => { window.__quit = 0; const rpc = Aurora.rpc;
      Aurora.rpc = (m, p) => m === 'quit' ? (window.__quit++, Promise.resolve({ok: true})) : rpc(m, p);
      Aurora.confirmClose(); }""")
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.scrim:not(.out) h3')].some(h => h.textContent === t)",
                              arg=ui.t("jobRunningTitle"), timeout=15000)
    assert ui.js("window.__quit") == 0
    ui.modal_button(ui.t("closeAnyway"))
    ui.page.wait_for_function("(t) => [...document.querySelectorAll('.scrim:not(.out) h3')].some(h => h.textContent === t)",
                              arg=ui.t("unsavedTitle"), timeout=30000)  # the job has stopped; the changes are offered for saving
    assert ui.rpc("state")["jobs"] == []
    ui.page.wait_for_selector(".scrim:not(.out) .job", state="detached", timeout=5000)
    ui.modal_button(ui.t("dontSave"))
    ui.page.wait_for_function("() => window.__quit === 1", timeout=5000)


def test_save_as_and_recent_files(ui):
    ui.open("plain")
    out = ui.tmp / "copy.pdf"
    ui.dialogs(out)
    ui.page.keyboard.press("Control+Shift+S")
    ui.page.wait_for_function("(p) => Aurora.S.doc.path === p", arg=str(out), timeout=15000)
    ui.page.click("#menuBtn")
    ui.page.locator("#menu .m-item", has_text=ui.t("closeDoc")).click()
    # the list is read again after closing; until then the one from before shows
    ui.page.wait_for_function("(n) => { const e = document.querySelector('#start .ritem .rn'); return e && e.textContent === n; }",
                              arg="copy.pdf", timeout=10000)


def test_a_file_another_program_changed_is_saved_as_a_new_file(ui):
    path = ui.open("plain")
    ui.js("Aurora.edit('rotate', {pages: [0], degrees: 90})")
    ui.idle()
    shutil.copy(ui.docs["report"], path)  # a newer version arrives in its place (a sync, a colleague)
    newer = Path(path).read_bytes()
    mine = ui.tmp / "mine.pdf"
    ui.dialogs(mine)
    ui.page.keyboard.press("Control+s")
    ui.page.wait_for_function("(p) => Aurora.S.doc.path === p", arg=str(mine), timeout=15000)
    assert Path(path).read_bytes() == newer and pymupdf.open(mine)[0].rotation == 90
    assert ui.t("e_FILE_CHANGED") in ui.toasts()


def test_print_renders_every_page_into_the_print_frame(ui):
    ui.open("report")
    ui.js("""() => { window.__printed = 0; const f = document.querySelector('#printFrame');
      f.addEventListener('load', () => { f.contentWindow.print = () => { window.__printed++; }; }); }""")
    ui.page.keyboard.press("Control+p")
    ui.page.wait_for_selector(".scrim #prSeg")
    ui.modal_button(ui.t("printGo"))
    ui.page.wait_for_function("() => window.__printed === 1", timeout=20000)
    n = ui.js("document.querySelector('#printFrame').contentDocument.querySelectorAll('img').length")
    assert n == 4
    # the words for all pages work in the range field too, and a range past the end stops at the last page.
    # Counted by the frame's "ready" message: a frame read from the cache may send it before its load event
    ui.js("() => { window.__ready = 0; addEventListener('message', (e) => { if (e.data && e.data.auroraPrintReady) window.__ready++; }); }")
    for k, (spec, pages) in enumerate((("sve", 4), ("2-30000000", 3)), start=1):
        ui.js("() => { Aurora.print(); }")  # the frame has the focus since the first print: no Ctrl+P
        ui.page.click('.scrim:not(.out) #prSeg [data-v="range"]')
        ui.page.fill(".scrim:not(.out) #prRange", spec)
        ui.modal_button(ui.t("printGo"))
        ui.page.wait_for_function("(k) => window.__ready === k", arg=k, timeout=20000)
        assert ui.js("document.querySelector('#printFrame').contentDocument.querySelectorAll('img').length") == pages


# ---------------------------------------------------------------- languages and misc

def test_languages_switch_everything(ui):
    ui.open("plain")
    langs = ui.js("Aurora.LANGS.map(([code]) => code)")
    assert len(langs) == 22 and langs[:3] == ["sr", "en", "no"]
    for lang in langs:
        ui.js("Aurora.openPanel('settings')")
        ui.page.select_option("#sLang", lang)
        save = ui.js(f"Aurora.I18N['{lang}'].save")
        ui.page.wait_for_timeout(200)
        assert ui.page.inner_text("#saveBtn").strip() == save
        # nothing on screen is an untranslated key
        raw = ui.js(r"""() => [...document.querySelectorAll('body *')].filter(e => e.children.length === 0)
            .map(e => e.textContent.trim()).filter(t => /^(e|t|op|an|mode|sig|st|p)_[A-Za-z]+$|^[a-z]+[A-Z][A-Za-z]+$/.test(t))""")
        assert raw == [], raw


def test_about_names_the_author_the_licence_and_the_code(ui):
    """AGPL-3.0 asks the program to show its licence and where its code is; the author is named too."""
    ui.js("Aurora.openPanel('settings')")
    ui.page.select_option("#sLang", "en")
    assert ui.page.inner_text("#sAuthorLine") == "Created and vibe-coded by Marko Veselinović. © 2026 Marko Veselinović"
    assert "GNU AGPL-3.0" in ui.page.inner_text("#panelInner") and "no warranty" in ui.page.inner_text("#panelInner")
    repo = "https://github.com/KoMAr38/aurora-pdf"
    urls = ui.js("[...document.querySelectorAll('#panelInner [data-url]')].map((b) => b.dataset.url)")
    assert urls == [repo, repo + "/blob/main/LICENSE", repo + "/blob/main/THIRD_PARTY_NOTICES.md"]
    # a click asks the backend to open the page; caught here so no browser starts during the test
    ui.js("""() => { const call = Aurora.call; Aurora.opened = [];
        Aurora.call = (m, p, o) => m === 'open_url' ? (Aurora.opened.push(p.url), Promise.resolve({ ok: true })) : call(m, p, o); }""")
    ui.page.click('#panelInner [data-url$="LICENSE"]')
    assert ui.js("Aurora.opened") == [repo + "/blob/main/LICENSE"]


def test_read_aloud_uses_only_voices_on_this_computer(ui):
    """An online voice would send the text to a speech service; the program promises files stay on the PC."""
    ui.js("Aurora.setLang('en')")
    fake = """(voices) => { window.spoken = [];
        window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
        const synth = Object.assign(new EventTarget(), {
          getVoices: () => voices, cancel: () => {}, speak: (u) => window.spoken.push(u.voice && u.voice.name) });
        Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: synth });
        window.fillVoices = (more) => { voices = more; synth.dispatchEvent(new Event('voiceschanged')); }; }"""
    online = {"name": "Online (Natural)", "lang": "en-US", "localService": False, "default": True}
    zira = {"name": "Zira", "lang": "en-US", "localService": True, "default": False}
    ui.js(fake, [online, zira])
    ui.js("Aurora.speak('Hello there')")
    assert ui.js("window.spoken") == ["Zira"]
    # Chromium gives no voices on the first ask and fills the list a moment later
    ui.js(fake, [])
    ui.js("(v) => { setTimeout(() => fillVoices(v), 100); return Aurora.speak('Hello there'); }", [online, zira])
    assert ui.js("window.spoken") == ["Zira"]
    ui.js(fake, [online])
    ui.js("Aurora.speak('Hello there')")
    assert ui.js("window.spoken") == []
    assert ui.t("noSpeech") in ui.toasts()


def test_theme_follows_windows_and_the_choice_is_remembered(ui):
    page = ui.page
    theme = lambda: ui.js("document.documentElement.dataset.theme")  # noqa: E731
    assert theme() == "dark"
    page.emulate_media(color_scheme="light")  # Windows switches to light apps
    page.wait_for_function("() => document.documentElement.dataset.theme === 'light'", timeout=5000)
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
    ui.page.wait_for_function("() => Aurora.S.current === 1", timeout=5000)


def test_text_from_a_pdf_never_becomes_markup(ui, tmp_path):
    """Comments and their authors, field names, values and options, bookmarks, attachments, metadata and the
    file name all reach the page as text. One missed escape put this markup on the page."""
    x = '"><img src=x onerror="window.__xss=(window.__xss||0)+1">'
    doc = pymupdf.open(ui.docs["plain"])
    page = doc[0]
    note = page.add_text_annot((300, 100), "note " + x)
    note.set_info(title="author " + x, content="content " + x)
    note.update()
    box = page.add_freetext_annot(pymupdf.Rect(60, 400, 300, 440), "free " + x)
    box.set_info(title=x)
    box.update()
    for name, kind, rect in ((x + "t", pymupdf.PDF_WIDGET_TYPE_TEXT, (60, 460, 260, 480)),
                             (x + "c", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, (60, 500, 260, 520))):
        w = pymupdf.Widget()
        w.field_type, w.field_name, w.rect = kind, name, pymupdf.Rect(rect)
        if kind == pymupdf.PDF_WIDGET_TYPE_COMBOBOX:
            w.choice_values = [[x, x + " label"], ["b", "b"]]
        w.field_value = x
        page.add_widget(w)
    doc.set_toc([[1, "bm " + x, 1], [2, x, 2]])
    doc.embfile_add(x + ".csv", b"a;b\n", filename=x + ".csv", desc=x)
    doc.set_metadata({k: x for k in ("title", "author", "subject", "keywords", "creator", "producer")})
    path = tmp_path / ("x.pdf" if os.name == "nt" else 'x"><img src=x onerror=window.__xss=7>.pdf')  # Windows takes no " < >
    doc.save(str(path))
    ui.open(str(path), copy=False)
    for tool in ("annotate", "info", "fillsign"):
        ui.panel(tool)
        ui.page.wait_for_timeout(600)
    for side in ("outline", "attach"):
        ui.page.click(f'#sideTabs [data-side="{side}"]')
        ui.page.wait_for_timeout(600)
    ui.tool("select")
    ui.click_at(0, 302, 102)  # the note: its author shows in the mini toolbar
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    ui.js("() => { Aurora.closeDoc(); }")  # the start screen lists the file by its name
    ui.page.wait_for_function("(n) => [...document.querySelectorAll('#start .ritem .rn')].some(e => e.textContent === n)",
                              arg=path.name, timeout=10000)
    assert ui.js("window.__xss === undefined") and ui.js("document.querySelectorAll('img[src=\"x\"]').length") == 0


# ---------------------------------------------------------------- audit fixes

def _key(ui, key, code, ctrl=False):
    ui.js("""([key, code, ctrl]) => document.body.dispatchEvent(new KeyboardEvent('keydown',
        { key, code, ctrlKey: ctrl, bubbles: true, cancelable: true }))""", [key, code, ctrl])


def test_shortcuts_work_on_cyrillic_greek_and_azerty_layouts(ui):
    """Ctrl+S on a Russian layout gives the key "ы"; the physical key S is meant."""
    ui.open("plain")
    ui.js("() => { window.hits = []; Aurora.save = () => hits.push('save'); Aurora.fit = (m) => hits.push(m); }")
    _key(ui, "ы", "KeyS", ctrl=True)
    _key(ui, "σ", "KeyS", ctrl=True)  # Greek
    _key(ui, "à", "Digit0", ctrl=True)  # French AZERTY Ctrl+0
    assert ui.js("window.hits") == ["save", "save", "width"]
    ui.page.mouse.click(700, 500)
    _key(ui, "з", "KeyP")  # P on a Russian layout
    assert ui.js("Aurora.S.mode") == "pen"
    _key(ui, "м", "KeyV")
    assert ui.js("Aurora.S.mode") == "select"


def test_long_file_names_wrap_in_toasts_and_dialogs(ui):
    name = "a" * 116 + ".pdf"
    ui.js("(n) => Aurora.toast(Aurora.t('savedTo', { name: n }))", name)
    ui.js("(n) => { Aurora.confirm('x', n); }", name)
    ui.page.wait_for_selector(".scrim .m-body p")
    sizes = ui.js("""() => [...document.querySelectorAll('.toast, .toast .msg, .modal, .m-body, .m-body p')]
        .map((e) => [e.scrollWidth, e.clientWidth])""")
    assert sizes and all(s <= c for s, c in sizes), sizes


def test_the_annotation_bar_is_only_moved_while_scrolling(ui):
    ui.open("report")
    ui.tool("rect")
    ui.drag(0, [(100, 200), (200, 260)])
    ui.idle()
    ui.tool("select")
    ui.click_at(0, 150, 201)
    ui.page.wait_for_selector("#annbar:not(.hidden)")
    top0 = ui.js("parseFloat(document.querySelector('#annbar').style.top)")
    ui.js("""() => { window.rebuilt = 0;
        new MutationObserver(() => rebuilt++).observe(document.querySelector('#annbar'), { childList: true }); }""")
    ui.js("() => { const v = document.querySelector('#viewer'); for (let k = 0; k < 10; k++) { v.scrollTop += 4; v.dispatchEvent(new Event('scroll')); } }")
    ui.page.wait_for_timeout(200)
    assert ui.js("window.rebuilt") == 0
    assert ui.js("parseFloat(document.querySelector('#annbar').style.top)") < top0


def test_plural_forms_of_russian_and_macedonian(ui):
    forms = ui.js("""() => { const out = {}; const t = Aurora.t; Aurora.t = (k) => k.slice(1);
        for (const [lang, ns] of [['ru', [1, 2, 5, 21, 22, 25, 11]], ['mk', [1, 2, 21, 11]]]) {
          Aurora.S.lang = lang; out[lang] = ns.map((n) => Aurora.plural(n, 'x')); }
        Aurora.t = t; Aurora.S.lang = 'sr'; return out; }""")
    assert forms == {"ru": ["One", "Few", "Many", "One", "Few", "Many", "Many"], "mk": ["One", "Few", "One", "Few"]}


def test_numbers_and_dates_format_in_every_language(ui):
    bad = ui.js("""() => { const bad = [];
        for (const [lang] of Aurora.LANGS) { Aurora.S.lang = lang;
          try { const n = Aurora.fmtNum(1234.5, 1), d = Aurora.fmtShort(new Date(2026, 9, 23));
                if (!n || !d) bad.push(lang); } catch (e) { bad.push(lang + ': ' + e); } }
        Aurora.S.lang = 'sr'; return bad; }""")
    assert bad == []


def test_a_long_word_fits_its_segment_button(ui):
    """Russian "Автоматически" ran out of its third of the theme switch."""
    ui.js("Aurora.setLang('ru')")
    ui.js("Aurora.openPanel('settings')")
    sizes = ui.js("() => [...document.querySelectorAll('#panel .seg, #panel .seg button')].map((e) => [e.scrollWidth, e.clientWidth])")
    assert sizes and all(s <= c for s, c in sizes), sizes
