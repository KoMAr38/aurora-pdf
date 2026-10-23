"""Page overlay data and markup editing.

* :func:`page_info` - everything the viewer draws on top of a rendered page: the text
  layer (for selection/copy), annotations, links and form fields, all in display space.
* annotation edits - ink, shapes, text markup, free text, notes, image stamps, redaction
  marks - created as real PDF annotations so they stay editable in other readers.
* form filling and flattening.
"""
from __future__ import annotations

import collections
import contextlib
import functools
import math
import re
import secrets
from typing import Sequence

import pymupdf

from .geom import (display_angle, hexcolor, page_ctm, point_to_unrot, rect_to_unrot, rgb, rounded, set_page_key,
                   size_unit, to_pdf, turn, vec_to_unrot)
from .pdftools import _prepare_png

SKIP_TYPES = {pymupdf.PDF_ANNOT_POPUP, pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET}
VERTEX_TYPES = {pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_LINE, pymupdf.PDF_ANNOT_POLYGON, pymupdf.PDF_ANNOT_POLY_LINE}
MARKUP_TYPES = {pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_UNDERLINE, pymupdf.PDF_ANNOT_STRIKE_OUT,
                pymupdf.PDF_ANNOT_SQUIGGLY}
RESIZABLE = {pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE, pymupdf.PDF_ANNOT_FREE_TEXT,
             pymupdf.PDF_ANNOT_STAMP, pymupdf.PDF_ANNOT_REDACT}
BORDERED = {pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE}

# Ff (field flags) bits we read
FF_READONLY = 1
FF_MULTILINE = 1 << 12
FF_PASSWORD = 1 << 13
FF_MULTISELECT = 1 << 21

# how MuPDF writes a one-line field whose text needs a font other than Helvetica (PyMuPDF 1.28.2):
# laid out top-down, in a single TJ, the text matrix scaled when the line is a little too wide
_TOP_DOWN = re.compile(
    rb"(?P<head>/Tx BMC\s+q\s.*?re\s+W\s+n\s+)1 0 0 -1 -?0 (?P<h>[\d.]+) cm\s+q\s+[-\d. ]+re\s+W\s+n\s+"
    rb"(?P<color>.*?)BT\s+(?P<font>/\S+ (?P<size>[\d.]+) Tf)\s+"
    rb"(?P<a>[\d.]+) 0 0 -(?P<d>[\d.]+) (?P<x>[-\d.]+) [-\d.]+ Tm\s+"
    rb"(?P<glyphs>\[[^\]]*\])\s*TJ\s+ET\s+Q\s+Q\s+EMC\s*$", re.S)
# how MuPDF writes a list box: every option from /TI on, one line each, clipped to the box; in the text's
# own font from the top down (Td), or in its other font (Č, Ć, Đ) in a top-down space of its own (cm, Tm)
_LIST = re.compile(
    rb"(?P<clip>[-\d.]+ [-\d.]+ [\d.]+ [\d.]+) (?P<cut>re\s+W\s+n\s+)(?:"
    rb"BT\s+(?:[\d. ]+(?:rg|g|k)\s+)?[-\d.]+ (?P<top>[-\d.]+) Td\s+0 -(?P<lh>[\d.]+) Td\s+/\S+ (?P<fs>[\d.]+) Tf"
    rb"|1 0 0 -1 -?0 (?P<h>[\d.]+) cm\s+q\s+[-\d. ]+re\s+W\s+n\s+"
    rb"(?:[\d. ]+(?:rg|g|k)\s+)?BT\s+/\S+ (?P<fs2>[\d.]+) Tf\s+"
    rb"[\d.]+ 0 0 -[\d.]+ [-\d.]+ (?P<first>[\d.]+) Tm\s+\[[^\]]*\]\s*TJ(?:\s+0 -(?P<lh2>[\d.]+) Td)?)")


# --- text layer -----------------------------------------------------------------


def _line_dirs(page: pymupdf.Page, tp) -> dict[tuple[int, int], tuple[float, float]]:
    dirs = {}
    for b in page.get_text("dict", textpage=tp)["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for li, ln in enumerate(b.get("lines", [])):
            dirs[(b["number"], li)] = ln["dir"]
    return dirs


def page_words(page: pymupdf.Page):
    """Words as PyMuPDF returns them (unrotated space). The text layer and the markup
    tools both index into this list, so they must use this one function."""
    tp = page.get_textpage()
    return page.get_text("words", textpage=tp), tp


def text_layer(page: pymupdf.Page) -> list[list]:
    """Per word: [left, top, length, thickness, angle, text, line_end] in display space.

    (left, top) is the corner where the text starts at the top of the glyphs; the span
    is laid out horizontally there, then rotated clockwise by ``angle``.
    """
    words, tp = page_words(page)
    if not words:
        return []
    dirs = _line_dirs(page, tp)
    m = turn(page)
    out = []
    for i, (x0, y0, x1, y1, txt, bno, lno, _wno) in enumerate(words):
        ang = display_angle(dirs.get((bno, lno), (1.0, 0.0)), page)
        r = pymupdf.Rect(x0, y0, x1, y1) * m
        if ang == 0:
            left, top, length, thick = r.x0, r.y0, r.width, r.height
        elif ang == 90:
            left, top, length, thick = r.x1, r.y0, r.height, r.width
        elif ang == 180:
            left, top, length, thick = r.x1, r.y1, r.width, r.height
        else:
            left, top, length, thick = r.x0, r.y1, r.height, r.width
        nxt = words[i + 1] if i + 1 < len(words) else None
        end = nxt is None or (nxt[5], nxt[6]) != (bno, lno)
        out.append([round(left, 2), round(top, 2), round(length, 2), round(thick, 2), ang, txt, int(end)])
    return out


# --- overlay data ---------------------------------------------------------------


def annot_info(page: pymupdf.Page, a: pymupdf.Annot) -> dict:
    t = a.type[0]
    info = a.info
    colors = a.colors or {}
    stroke, fill = colors.get("stroke"), colors.get("fill")
    op = a.opacity
    return {
        "x": a.xref,
        "t": a.type[1],
        "r": rounded(a.rect * page.rotation_matrix),
        "c": hexcolor(stroke) or hexcolor(fill),
        "f": hexcolor(fill),
        "o": round(op, 2) if op is not None and op >= 0 else 1,
        "w": round(((a.border or {}).get("width") or 0) / size_unit(page), 2),  # as on screen, /UserUnit or not
        "txt": info.get("content", "") if info.get("name") != "ImageStamp" else "",
        "by": info.get("title", ""),
        "dt": info.get("modDate") or info.get("creationDate") or "",
        "mv": t not in MARKUP_TYPES,
        "rs": t in RESIZABLE,
        "img": info.get("name") == "ImageStamp",
    }


def page_annots(page: pymupdf.Page) -> list[dict]:
    return [annot_info(page, a) for a in page.annots() if a.type[0] not in SKIP_TYPES]


def page_links(page: pymupdf.Page) -> list[dict]:
    """Links in display space. A link inside the document names its target page object ("px", the
    xref the document summary lists per page), not its position: the overlay of this page is cached
    under its revision, which stays the same when other pages move."""
    out = []
    for link in page.get_links():
        r = rounded(link["from"])  # MuPDF gives a freshly loaded page's links in display space
        kind = link.get("kind")
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and link.get("page", -1) >= 0:
            out.append({"r": r, "px": page.parent.page_xref(link["page"])})
        elif kind == pymupdf.LINK_URI and link.get("uri"):
            out.append({"r": r, "u": link["uri"]})
    return out


def _widget_kind(w) -> str:
    return {
        pymupdf.PDF_WIDGET_TYPE_TEXT: "text",
        pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "check",
        pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
        pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "combo",
        pymupdf.PDF_WIDGET_TYPE_LISTBOX: "list",
        pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature",
        pymupdf.PDF_WIDGET_TYPE_BUTTON: "button",
    }.get(w.field_type, "other")


def _widgets(page: pymupdf.Page):
    """The page's widgets in one walk. page.widgets() looks each one up from the start of the page again
    (PyMuPDF 1.28), which takes seconds on a page with a thousand fields."""
    w = page.first_widget
    while w:
        yield w
        w = w.next


def page_fields(page: pymupdf.Page) -> list[dict]:
    out = []
    m = page.rotation_matrix
    for w in _widgets(page):
        kind = _widget_kind(w)
        flags = w.field_flags or 0
        d = {
            "x": w.xref,
            "n": w.field_name or "",
            "k": kind,
            "r": rounded(pymupdf.Rect(w.rect) * m),
            "v": w.field_value if w.field_value is not None else "",
            "ro": bool(flags & FF_READONLY),
            "fs": round((w.text_fontsize or 0) / size_unit(page), 2),
        }
        if kind in ("check", "radio"):  # as its drawing shows it: two widgets of a field can differ
            d["v"] = page.parent.xref_get_key(w.xref, "AS")[1] not in ("null", "/Off")
        elif kind in ("combo", "list"):
            # [value, label]: a choice may store "NO" and show "Norway"; the file keeps the value
            d["opts"] = [[o, o] if isinstance(o, str) else [o[0], o[-1]] for o in (w.choice_values or [])]
            d["multi"] = bool(flags & FF_MULTISELECT)
            if d["multi"]:
                d["v"] = _chosen(page.parent, w.xref)
        elif kind == "signature":
            d["signed"] = bool(w.is_signed)
        elif kind == "text":
            d["ml"] = bool(flags & FF_MULTILINE)
            d["pw"] = bool(flags & FF_PASSWORD)
            d["max"] = w.text_maxlen or 0
        out.append(d)
    return out


def page_info(page: pymupdf.Page) -> dict:
    """Everything the viewer overlays on one rendered page."""
    return {
        "w": round(page.rect.width, 2),
        "h": round(page.rect.height, 2),
        "rot": page.rotation,
        "words": text_layer(page),
        "annots": page_annots(page),
        "links": page_links(page),
        "fields": page_fields(page),
    }


def doc_annots(doc: pymupdf.Document) -> list[dict]:
    """Every annotation in the document (for the comments list), page by page."""
    return [{**d, "p": pno} for pno, page in enumerate(doc) for d in page_annots(page)]


# --- creating annotations ----------------------------------------------------------


def _finish(annot: pymupdf.Annot, author: str, *, stroke=None, fill=None, width=None,
            opacity=None, content=None, update: bool = True) -> int:
    if stroke is not None or fill is not None:
        annot.set_colors(stroke=stroke, fill=fill)
    if width is not None:  # sizes come as on screen, the file keeps them in units of its page (/UserUnit)
        annot.set_border(width=width * size_unit(annot.parent))
    if opacity is not None and opacity < 1:
        annot.set_opacity(opacity)
    now = pymupdf.get_pdf_now()
    kw = dict(title=author or "", creationDate=now, modDate=now)
    if content is not None:
        kw["content"] = content
    annot.set_info(**kw)
    if update:
        annot.update()
    return annot.xref


def add_ink(page, strokes: Sequence[Sequence[Sequence[float]]], *, color="#e11d48", width=2.0,
            opacity=1.0, author="") -> int:
    lines = []
    for s in strokes:
        pts = [tuple(point_to_unrot(p, page)) for p in s]
        if len(pts) == 1:  # a click: draw a dot
            x, y = pts[0]
            pts.append((x + 0.01, y + 0.01))
        if pts:
            lines.append(pts)
    if not lines:
        raise ValueError("empty ink")
    a = page.add_ink_annot(lines)
    return _finish(a, author, stroke=rgb(color), width=width, opacity=opacity)


def add_shape(page, kind: str, coords: Sequence[float], *, color="#2563eb", fill=None, width=2.0,
              opacity=1.0, author="") -> int:
    """kind: rect | ellipse (coords = display rect) or line | arrow (coords = x1,y1,x2,y2)."""
    if kind in ("rect", "ellipse"):
        r = rect_to_unrot(coords, page)
        if r.width < 2 or r.height < 2:
            raise ValueError("shape too small")
        a = page.add_rect_annot(r) if kind == "rect" else page.add_circle_annot(r)
        return _finish(a, author, stroke=rgb(color), fill=rgb(fill) if fill else None, width=width, opacity=opacity)
    if kind in ("line", "arrow"):
        p1 = point_to_unrot(coords[:2], page)
        p2 = point_to_unrot(coords[2:4], page)
        if abs(p1 - p2) < 2:
            raise ValueError("line too short")
        a = page.add_line_annot(p1, p2)
        if kind == "arrow":
            a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
        return _finish(a, author, stroke=rgb(color), fill=rgb(color) if kind == "arrow" else None,
                       width=width, opacity=opacity)
    raise ValueError(f"unknown shape: {kind}")


def _quad_for(r: pymupdf.Rect, direction) -> pymupdf.Quad:
    """Quad over a text line, oriented along its writing direction (unrotated space)."""
    dx, dy = direction
    if abs(dx) >= abs(dy):
        if dx >= 0:
            return pymupdf.Quad(r.tl, r.tr, r.bl, r.br)
        return pymupdf.Quad(r.br, r.bl, r.tr, r.tl)
    if dy < 0:  # running upwards
        return pymupdf.Quad(r.bl, r.tl, r.br, r.tr)
    return pymupdf.Quad(r.tr, r.br, r.tl, r.bl)


MARKUP_ADDERS = {
    "highlight": "add_highlight_annot",
    "underline": "add_underline_annot",
    "strikeout": "add_strikeout_annot",
}


def add_markup(page, kind: str, word_indices: Sequence[int], *, color=None, opacity=1.0, author="") -> int:
    """Highlight/underline/strike the given words (indices into :func:`page_words`)."""
    if kind not in MARKUP_ADDERS:
        raise ValueError(f"unknown markup: {kind}")
    words, tp = page_words(page)
    chosen = [words[i] for i in sorted(set(int(i) for i in word_indices)) if 0 <= i < len(words)]
    if not chosen:
        raise ValueError("no words selected")
    dirs = _line_dirs(page, tp)
    groups: dict[tuple[int, int], pymupdf.Rect] = {}
    for x0, y0, x1, y1, _t, bno, lno, _w in chosen:
        key = (bno, lno)
        groups[key] = groups[key] | pymupdf.Rect(x0, y0, x1, y1) if key in groups else pymupdf.Rect(x0, y0, x1, y1)
    quads = [_quad_for(r, dirs.get(k, (1.0, 0.0))) for k, r in groups.items()]
    a = getattr(page, MARKUP_ADDERS[kind])(quads=quads)
    default = {"highlight": "#facc15", "underline": "#16a34a", "strikeout": "#dc2626"}[kind]
    text = " ".join(w[4] for w in chosen)
    return _finish(a, author, stroke=rgb(color or default), opacity=opacity, content=text[:2000])


def add_freetext(page, rect: Sequence[float], text: str, *, fontsize=12.0, color="#111827", fill=None,
                 author="") -> int:
    if not text.strip():
        raise ValueError("empty text")
    r = rect_to_unrot(rect, page)
    a = page.add_freetext_annot(
        r, text, fontsize=fontsize * size_unit(page), fontname="helv", text_color=rgb(color),
        fill_color=rgb(fill) if fill else None, rotate=page.rotation,
    )
    return _finish(a, author)


def add_note(page, point: Sequence[float], text: str, *, color="#facc15", author="") -> int:
    # shortcut: MuPDF draws a note's icon 16 units big whatever its /Rect, flags or drawing say, so on a page with
    # a /UserUnit it shows that many times bigger; other readers keep such an icon one size on screen anyway
    a = page.add_text_annot(point_to_unrot(point, page), text or "", icon="Comment")
    return _finish(a, author, stroke=rgb(color), content=text or "")


def add_image_stamp(page, rect: Sequence[float], image: bytes, *, author="", label="") -> int:
    """Place an image (e.g. a handwritten signature) as a movable stamp annotation."""
    png, _w, _h = _prepare_png(image, 1.0, rotate=page.rotation)
    a = page.add_stamp_annot(rect_to_unrot(rect, page), stamp=png)
    return _finish(a, author, content=label or None, update=False)


def mark_redaction(page, rect: Sequence[float], *, author="") -> int:
    r = rect_to_unrot(rect, page)
    if r.width < 1 or r.height < 1:
        raise ValueError("area too small")
    a = page.add_redact_annot(r, fill=(0, 0, 0))
    return _finish(a, author)


def mark_redaction_text(doc: pymupdf.Document, query: str, *, author="") -> int:
    """Mark every occurrence of ``query`` for redaction. Returns the number of marks."""
    n = 0
    for page in doc:
        # without TEXT_PRESERVE_LIGATURES (on by default): "fi" finds a word written with the ﬁ glyph
        for r in page.search_for(query, flags=pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_PRESERVE_WHITESPACE
                                 | pymupdf.TEXT_MEDIABOX_CLIP):
            a = page.add_redact_annot(r, fill=(0, 0, 0))
            _finish(a, author, content=query)  # the query: applying the mark takes it out of bookmarks too
            n += 1
    return n


def apply_redactions(doc: pymupdf.Document) -> int:
    """Burn in every redaction mark: content under it is removed for good.

    MuPDF removes text, image pixels and line art. Comments, form fields and links that
    touch a mark go too, because they can hold the same words (a highlight stores them).
    A field whose last widget goes loses its value, which its parent object keeps, a radio
    or check box group whose chosen button goes loses that choice, and a hybrid form loses
    its XFA packet, which holds every value and word of the form again. A tagged page says
    its words again for copying and screen readers: what it says for content under a mark goes too.
    """
    n, heads, gone, pages, under, unsure, moved, pictures = 0, set(), set(), set(), {}, set(), {}, set()
    queries = {a.info["content"] for page in doc for a in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])
               if a.info["content"]}
    for page in doc:
        count, marks, taken = _marks(page)
        if not count:
            continue
        n += count
        pages.add(page.xref)
        before = {x for x, _, _ in doc.page_annot_xrefs(page.number) if x}  # 0: written into /Annots itself

        def hit(r) -> bool:
            return any(pymupdf.Rect(r).intersects(m) for m in marks)

        for xref in [a.xref for a in page.annots() if a.type[0] != pymupdf.PDF_ANNOT_REDACT and hit(a.rect)]:
            page.delete_annot(page.load_annot(xref))
        widgets = [w.xref for w in _widgets(page) if hit(w.rect)]
        heads |= {_field_object(doc, x) for x in widgets} - set(widgets)
        for xref in widgets:
            page.delete_widget(page.load_widget(xref))
        _drop_inline(doc, page, hit)
        forms, images = _drawings(doc, page.number)
        glyphs = _placed(page)  # as the page stands: the check after the rewrite covers what _steady writes too
        steadied, lent = _steady_page(doc, page)  # what MuPDF writes out anew, made such that it keeps it in place
        plan = _alt_under(doc, page, marks)  # before MuPDF rewrites the page, while the words are there
        for xref in forms:  # MuPDF draws the page from copies of its drawings, which keep this key
            doc.xref_set_key(xref, _COPY_OF, str(xref))
        # MuPDF removes the links that touch a mark (a check of our own read their display rects as unrotated
        # and took links elsewhere on a turned page)
        _redact(doc, page)
        copies, shown = _drawings(doc, page.number)
        own = {}  # copy -> original, of the drawings MuPDF made for this page
        for xref in copies | forms:
            kind, orig = doc.xref_get_key(xref, _COPY_OF)
            if xref in copies and kind == "int":
                moved.setdefault((page.xref, int(orig)), xref)
                if xref != int(orig):
                    own[xref] = int(orig)
            pymupdf.mupdf.pdf_dict_dels(pymupdf.mupdf.pdf_load_object(pymupdf._as_pdf_document(doc), xref), _COPY_OF)
        for copy, orig in own.items():  # see _sets_first: the copy sets the render mode MuPDF took it to start with
            if _sets_first(doc.xref_stream(orig) or b"", b"Tr"):
                _write(doc, copy, b"0 Tr " + (doc.xref_stream(copy) or b""))
        if _shifted(glyphs, _placed(page), taken):  # the rewrite moved or lost text away from the marks
            raise ValueError("REDACT_MOVED")
        pictures |= {(page.xref, x) for x in images - shown}  # MuPDF put a picture without the pixels in its place
        gone |= before - {x for x, _, _ in doc.page_annot_xrefs(page.number) if x}
        said = None if plan is None else _alt_settle(doc, page, plan)
        if said is None:  # some marked content could not be placed: all the page says again goes
            _drop_alt_text(doc, page)
            unsure.add(page.xref)
            if plan is not None and _left(doc, page, plan["tag"]):
                raise RuntimeError("redaction: a mark of its own stayed on the page")
        else:
            for key, drop in said.items():
                under[key] = under.get(key, False) or drop
        _out_of_reach(doc, page, taken, set(own), set(own.values()))
        drawn = {x for x, *_ in _drawn_streams(doc, page)}
        for xref, data in steadied.items():  # the originals, which other pages and comments draw, as they were
            if xref not in drawn:
                _write(doc, xref, data)
        for xref in lent:
            pymupdf.mupdf.pdf_dict_dels(pymupdf.mupdf.pdf_load_object(pymupdf._as_pdf_document(doc), xref), "Resources")
    if n:
        _release(doc, pages, {x for _, x in pictures} | {x for _, x in moved})
    if heads:
        fields = _fields_of(doc)
        for field in heads:
            if field not in fields:  # no widget of it is left on any page
                for key in ("V", "DV", "RV", "I"):
                    _drop(doc, field, key)
                continue
            # a radio or check box group names the chosen button: keep it only while a button left shows it
            # (buttons can share one drawing of every state, so what a button can show proves nothing); the
            # default names another button as a rule, and goes only when it repeats the choice
            shown = {doc.xref_get_key(w, "AS")[1] for _, w in fields[field]}
            kind, value = doc.xref_get_key(field, "V")
            if kind == "name" and value != "/Off" and value not in shown:
                _drop(doc, field, "V")
                if doc.xref_get_key(field, "DV") == (kind, value):
                    _drop(doc, field, "DV")
    if n:
        # a tagged file points at its comments and fields from the structure tree, which keeps their objects
        # (and the words in them) in the file after they left the page: empty them
        left = {x for pno in range(len(doc)) for x, _, _ in doc.page_annot_xrefs(pno)}
        streams = set().union(*(_appearances(doc, x) for x in gone - left))  # what drew them
        for xref in gone - left - set(_fields_of(doc)):  # a widget can be the field of widgets left elsewhere
            doc.update_object(xref, "<<>>")
        _drop_structure_text(doc, pages, under, unsure, gone | streams, moved, pictures)
        _drop_xfa(doc)
        _redact_titles(doc, queries)
    return n


def _redact_titles(doc, queries: set[str]) -> None:
    """Each query (a search redaction's /Contents) in a bookmark title becomes █, as case-blind as search_for.
    The bookmark keeps its target; a full save drops the old title with the old object."""
    if not queries:
        return
    pattern = re.compile("|".join(map(re.escape, sorted(queries, key=len, reverse=True))), re.I)
    kind, val = doc.xref_get_key(doc.pdf_catalog(), "Outlines/First")
    todo, seen = [int(val.split()[0])] if kind == "xref" else [], set()
    while todo:
        xref = todo.pop()
        if xref in seen:
            continue
        seen.add(xref)
        kind, title = doc.xref_get_key(xref, "Title")
        if kind == "string" and pattern.search(title):
            doc.xref_set_key(xref, "Title", pymupdf.get_pdf_str(pattern.sub("\u2588", title)))
        for key in ("First", "Next"):
            kind, val = doc.xref_get_key(xref, key)
            if kind == "xref":
                todo.append(int(val.split()[0]))


_COPY_OF = "AuroraCopyOf"  # on a drawing for the length of one redaction: the xref it was copied from


def _redact(doc, page) -> None:
    """MuPDF's redaction of the page (text, picture pixels and line art under the marks), then each mark's colour
    (/IC; none, no fill) over what it took: its /QuadPoints, else its /Rect, in PDF space as the mark holds them.
    PyMuPDF paints that fill through page geometry of its own, which put it elsewhere on a turned page whose
    boxes start away from 0,0 and on a page with a /UserUnit.
    shortcut: a mark's /OverlayText (words another program shows on the fill) is not drawn."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    paint = []
    for a in page.annots(types=[pymupdf.PDF_ANNOT_REDACT]):
        obj = m.pdf_load_object(pdf, a.xref)
        ic, qp = m.pdf_dict_gets(obj, "IC"), m.pdf_dict_gets(obj, "QuadPoints")
        color = [m.pdf_array_get_real(ic, i) for i in range(m.pdf_array_len(ic))] if m.pdf_is_array(ic) else []
        op = {1: b"g", 3: b"rg", 4: b"k"}.get(len(color))
        if op:
            boxes = [m.fz_rect_from_quad(m.pdf_to_quad(qp, i)) for i in range(0, m.pdf_array_len(qp) - 7, 8)] \
                if m.pdf_is_array(qp) else []
            paint.append(b" ".join(map(_decimal, color)) + b" " + op + b" " + b" ".join(
                b" ".join(map(_decimal, (r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0))) + b" re f"
                for r in boxes or [m.pdf_dict_get_rect(obj, m.PdfObj("Rect"))]))
    opts = m.PdfRedactOptions()
    opts.black_boxes, opts.text = 0, m.PDF_REDACT_TEXT_REMOVE
    opts.image_method, opts.line_art = m.PDF_REDACT_IMAGE_PIXELS, m.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED
    m.pdf_redact_page(pdf, pymupdf._as_pdf_page(page), opts)
    if paint:
        page.wrap_contents()  # the fill in PDF space, whatever the page's content left the drawing state at
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, b"q " + b" ".join(paint) + b" Q")
        parts = m.pdf_new_array(pdf, 2)
        for x in [*page.get_contents(), xref]:
            m.pdf_array_push(parts, m.pdf_new_indirect(pdf, x, 0))
        set_page_key(page, "Contents", parts)


def _drawings(doc, pno: int) -> tuple[set[int], set[int]]:
    """The xrefs of the drawings (form XObjects) and of the pictures a page uses, also inside its drawings."""
    return {x[0] for x in doc.get_page_xobjects(pno)}, {x[0] for x in doc.get_page_images(pno, full=True)}


def _marks(page) -> tuple[int, list[pymupdf.Rect], list[pymupdf.Rect]]:
    """The number of redaction marks on a page, the areas they cover and the areas MuPDF takes away. A mark made
    elsewhere can have /QuadPoints: MuPDF takes what is under those then, and the mark covers its /Rect too."""
    marks = list(page.annots(types=[pymupdf.PDF_ANNOT_REDACT]))
    areas, taken = [], []
    for a in marks:
        v = a.vertices or []
        quads = [pymupdf.Quad(*v[i:i + 4]).rect for i in range(0, len(v) - 3, 4)]
        areas += [pymupdf.Rect(a.rect)] + quads
        taken += quads or [pymupdf.Rect(a.rect)]
    return len(marks), areas, taken


def _write(doc, xref: int, data: bytes) -> None:
    """update_stream, without the stream's /DecodeParms: they belong to its old data, and update_stream keeps
    them (a PNG predictor there turns the new data into garbage)."""
    pymupdf.mupdf.pdf_dict_dels(pymupdf.mupdf.pdf_load_object(pymupdf._as_pdf_document(doc), xref), "DecodeParms")
    doc.update_stream(xref, data)


def _release(doc, pages: set[int], olds: set[int]) -> None:
    """MuPDF draws a redacted page from new pictures and drawings without what was under a mark, but resources
    that pages share, or that the page tree hands down to its pages, still name the old ones (``olds``), which
    keeps them in the file. Each other page that names one it does not draw gets resources of its own without it
    (its content stays as it is: MuPDF's sanitize filter, which told this before, moved text), and page tree
    resources that no page takes any more go."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    for pno in range(len(doc)):
        xref = doc.page_xref(pno)
        unused = set() if xref in pages else _named(doc, xref) & olds
        if unused:
            unused -= _drawn_xobjects(doc, doc[pno])
        if unused:
            page = m.pdf_load_object(pdf, xref)
            res = m.pdf_copy_dict(m.pdf_dict_get_inheritable(page, m.PdfObj(m.PDF_ENUM_NAME_Resources)))
            names = m.pdf_copy_dict(m.pdf_dict_gets(res, "XObject"))
            for i in reversed(range(m.pdf_dict_len(names))):
                if m.pdf_to_num(m.pdf_dict_get_val(names, i)) in unused:
                    m.pdf_dict_del(names, m.pdf_dict_get_key(names, i))
            m.pdf_dict_puts(res, "XObject", names)
            m.pdf_dict_puts(page, "Resources", res)
    used, nodes, leaves = set(), set(), {doc.page_xref(pno) for pno in range(len(doc))}
    for leaf in leaves:
        owner, num = 0, leaf
        for _ in range(64):  # up the page tree: the first node with /Resources hands them to the page
            node = m.pdf_load_object(pdf, num)
            if not owner and m.pdf_is_dict(m.pdf_dict_gets(node, "Resources")):
                owner = num
            nodes.add(num)
            num = m.pdf_to_num(m.pdf_dict_gets(node, "Parent"))
            if not num:
                break
        used.add(owner)
    for num in nodes - used - leaves:
        m.pdf_dict_dels(m.pdf_load_object(pdf, num), "Resources")


def _drawn_xobjects(doc, page) -> set[int]:
    """The xrefs of the pictures and drawings a page draws (Do), also inside its drawings."""
    m = pymupdf.mupdf
    return {m.pdf_to_num(_resource(res, "XObject", data, args[-1]))
            for _, _, res, data, _ in _drawn_streams(doc, page) for _, args, *_ in _operators(data, (b"Do",), res)
            if args}


def _named(doc, xref: int) -> set[int]:
    """The xrefs a page names in its resources for drawings and pictures, its own or handed down."""
    m = pymupdf.mupdf
    page = m.pdf_load_object(pymupdf._as_pdf_document(doc), xref)
    res = m.pdf_dict_get_inheritable(page, m.PdfObj(m.PDF_ENUM_NAME_Resources))
    xobj = m.pdf_dict_gets(res, "XObject") if m.pdf_is_dict(res) else m.PdfObj()
    n = m.pdf_dict_len(xobj) if m.pdf_is_dict(xobj) else 0
    return {m.pdf_to_num(m.pdf_dict_get_val(xobj, i)) for i in range(n)}


def _text_under(page, areas: list) -> bool:
    """Whether a glyph still lies under an area MuPDF took (its middle inside one, white space aside): in the page's
    content, in the drawings it uses, in what it fills through a pattern or in a soft mask, in layers that are off
    too. Text comes unrotated, as marks do (see _text_page).
    The middle of its box from the font's measures counts, as MuPDF reads it, and the middle of what it draws, a
    point inside an area's edge (a font with wrong measures, as a Type3 font can have, draws its glyphs away from
    that box, where MuPDF misses them; a mark above a letter of the next line reaches in a little, rightly left).
    The whole page is read: with a clip MuPDF reports a glyph it removed as one of no width at the clip's edge."""
    near = pymupdf.Rect(areas[0])
    for r in areas[1:]:
        near |= r
    flags = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_IGNORE_ACTUALTEXT | pymupdf.TEXT_MEDIABOX_CLIP
    inner = [pymupdf.Rect(r) + (1, 1, -1, -1) for r in areas]
    for how, inside in ((flags, areas), (flags | pymupdf.TEXT_ACCURATE_BBOXES, inner)):
        for block in _text_page(page, how).extractRAWDICT()["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    if x0 > near.x1 or x1 < near.x0 or y0 > near.y1 or y1 < near.y0:
                        continue
                    for ch in span["chars"]:
                        x0, y0, x1, y1 = ch["bbox"]
                        x, y = (x0 + x1) / 2, (y0 + y1) / 2
                        if not ch["c"].isspace() and any(r.x0 < x < r.x1 and r.y0 < y < r.y1 for r in inside):
                            return True
    return False


def _run_all(page, dev, whole: bool = False) -> None:
    """Run the page's own content (``whole``: with its comments and fields) through ``dev``, then close it: with
    every layer shown and in the space marks are read in. Layers that are off hide text from the page as shown, not
    from MuPDF's redaction, which writes them out anew too (and leaves a pattern in one as it is). And MuPDF turns
    the page back about its size as it draws it, while marks are read through page.derotation_matrix, which goes by
    the CropBox as written: on a turned page with a /UserUnit the two lie apart."""
    m = pymupdf.mupdf
    mt = page.derotation_matrix
    run = m.pdf_run_page_with_usage if whole else m.pdf_run_page_contents_with_usage
    try:  # no usage: nothing is hidden
        run(pymupdf._as_pdf_page(page), dev, m.FzMatrix(mt.a, mt.b, mt.c, mt.d, mt.e, mt.f), None, m.FzCookie())
    finally:
        m.fz_close_device(dev)


def _text_page(page, flags: int) -> pymupdf.TextPage:
    """page.get_textpage(flags=flags), but read as _run_all reads the page: text in a layer that is off, or under a
    mark on a turned page with a /UserUnit, was looked for elsewhere."""
    m = pymupdf.mupdf
    tp = m.FzStextPage(m.FzRect(*(page.rect * page.derotation_matrix)))
    _run_all(page, m.fz_new_stext_device(tp, m.FzStextOptions(flags)), whole=True)
    return pymupdf.TextPage(tp)


class _Glyphs(pymupdf.mupdf.FzDevice2):
    """The glyphs drawn through it (filled, stroked, clipped with, as a pattern fills them, or unseen) as
    (glyph, x, y, box, how drawn, colour, opacity), in ``found``. The box is the one MuPDF's redaction reads
    (pdf_redact_text_filter): from the font's descender to its ascender over the glyph's advance, through the
    glyph's matrix, a tenth of it in from each side; None where that box has no inside, and for a glyph of text
    written downwards: MuPDF reads that box from where the pen stands, a glyph's height above where the glyph is
    drawn, and takes the glyph after the one under a mark. What it takes there is not what lies under the mark, so
    such a glyph lost stops the redaction (_shifted): upright text cannot be redacted."""

    def __init__(self):
        super().__init__()
        for name in ("fill_text", "stroke_text", "clip_text", "clip_stroke_text", "ignore_text"):
            getattr(self, "use_virtual_" + name)()
        self.found = []

    def _text(self, text, ctm, how: str, cs=None, color=None, alpha: float = 1.0) -> None:
        m = pymupdf.mupdf
        n = m.ll_fz_colorspace_n(cs) if cs and color else 0
        paint = tuple(round(m.floats_getitem(color, i), 3) for i in range(n)), round(alpha, 3)
        ca, cb, cc, cd, ce, cf = ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f
        span = text.head
        while span:
            font, t, vertical = span.font, span.trm, span.wmode
            a, b = t.a * ca + t.b * cc, t.a * cb + t.b * cd  # from glyph space to here, the origin aside
            c, d = t.c * ca + t.d * cc, t.c * cb + t.d * cd
            down, up = m.ll_fz_font_descender(font), m.ll_fz_font_ascender(font)
            items, box = m.FzTextSpan(span), None
            for i in range(span.len):
                it = items.items(i)
                ox, oy = it.x * ca + it.y * cc + ce, it.x * cb + it.y * cd + cf
                if not (math.isfinite(ox) and math.isfinite(oy)):
                    continue
                if it.gid < 0 and i:  # more letters of the glyph before (a ligature): they go with it
                    self.found.append((it.gid, ox, oy, box, how, *paint))
                    continue
                adv = m.ll_fz_advance_glyph(font, it.gid, 0) if it.gid >= 0 else 0.0
                x0, x1, y0, y1 = 0.0, adv, down, up
                box = None
                if not vertical and x0 <= x1 and y0 <= y1:
                    xs = (ox + x0 * a + y0 * c, ox + x1 * a + y0 * c, ox + x0 * a + y1 * c, ox + x1 * a + y1 * c)
                    ys = (oy + x0 * b + y0 * d, oy + x1 * b + y0 * d, oy + x0 * b + y1 * d, oy + x1 * b + y1 * d)
                    w, h = (max(xs) - min(xs)) / 10, (max(ys) - min(ys)) / 10
                    box = (min(xs) + w, min(ys) + h, max(xs) - w, max(ys) - h)
                self.found.append((it.gid, ox, oy, box, how, *paint))
            span = span.next

    def fill_text(self, ctx, text, ctm, cs, color, alpha, *args):
        self._text(text, ctm, "fill", cs, color, alpha)

    def stroke_text(self, ctx, text, stroke, ctm, cs, color, alpha, *args):
        self._text(text, ctm, "stroke", cs, color, alpha)

    def clip_text(self, ctx, text, ctm, scissor):
        self._text(text, ctm, "clip")

    def clip_stroke_text(self, ctx, text, stroke, ctm, scissor):
        self._text(text, ctm, "clip stroke")

    def ignore_text(self, ctx, text, ctm):  # invisible text, as an OCR layer has
        self._text(text, ctm, "unseen")


def _placed(page) -> list[tuple]:
    """The glyphs the page's own content draws (comments and fields aside), as _Glyphs tells them, read as
    _run_all reads the page."""
    dev = _Glyphs()
    _run_all(page, dev)
    return dev.found


def _shifted(before: list, after: list, areas: list) -> bool:
    """Whether a rewrite of the page drew a glyph that was not there before (in place and way of drawing), or lost
    one that MuPDF's redaction does not take: one whose box (see _Glyphs) touches none of ``areas``, what MuPDF
    took."""
    def place(g) -> tuple:
        return g[0], *g[4:], g[1], g[2]

    grids = {}  # built on the first glyph that is not in the same place

    def near(glyphs: list, g) -> bool:  # within a hair, in the nine cells around it
        if id(glyphs) not in grids:
            grids[id(glyphs)] = cells = collections.defaultdict(list)
            for h in glyphs:
                cells[h[0], *h[4:], round(h[1]), round(h[2])].append(h)
        return any(abs(h[1] - g[1]) <= 0.05 and abs(h[2] - g[2]) <= 0.05 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for h in grids[id(glyphs)].get((g[0], *g[4:], round(g[1]) + dx, round(g[2]) + dy), ()))

    def taken(box) -> bool:  # MuPDF works in floats: a hair's room
        return box is not None and any(box[0] <= r.x1 + 0.01 and r.x0 - 0.01 <= box[2] and box[1] <= r.y1 + 0.01
                                       and r.y0 - 0.01 <= box[3] for r in areas)

    # the same place first (nearly every glyph): the cells around it only for the rest
    was, now = {place(g) for g in before}, {place(g) for g in after}
    return (any(place(g) not in was and not near(before, g) for g in after)
            or any(place(g) not in now and not near(after, g) and not taken(g[3]) for g in before))


def _out_of_reach(doc, page, areas: list, own: set[int], olds: set[int]) -> None:
    """MuPDF's redaction does not reach what a page fills an area with through a tiling pattern (a drawing of its
    own, repeated tile by tile) or what a soft mask draws (the /SMask of an ExtGState: a drawing whose light or
    cover tells how much shows of what is drawn under it): text there stays under the mark, and such a drawing can
    draw an original of a drawing the page now draws a copy of (``olds``), with what was under the mark. Such
    patterns and masks are swapped, in the resources of the page and of the copies it draws (``own``), for copies
    without text. A glyph still under a mark after that stops the redaction (``areas``: what MuPDF took).
    shortcut: all text of such a pattern or mask goes, also away from the mark (where a tile or a mask lands takes
    the matrix in force where it is used), and a picture in it stays as it is."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    left = _text_under(page, areas)
    if not (left or olds):
        return
    done = {}
    for xref in (page.xref, *own):
        obj = m.pdf_load_object(pdf, xref)
        res = m.pdf_dict_get_inheritable(obj, m.PdfObj(m.PDF_ENUM_NAME_Resources))
        res = _without_text_in(doc, res, done, 0, lambda drawing: left or bool(_reach(drawing) & olds))
        if res is not None:  # resources of its own: they can be shared
            m.pdf_dict_puts(obj, "Resources", res)
    if left and _text_under(page, areas):
        raise ValueError("REDACT_LEFT")


def _tiling(obj) -> bool:
    m = pymupdf.mupdf
    return m.pdf_is_stream(obj) and m.pdf_to_int(m.pdf_dict_gets(obj, "PatternType")) == 1


def _mask(gs):
    """The drawing (a transparency group) of the soft mask an ExtGState sets, or None."""
    m = pymupdf.mupdf
    smask = m.pdf_dict_gets(gs, "SMask") if m.pdf_is_dict(gs) else m.PdfObj()
    group = m.pdf_dict_gets(smask, "G") if m.pdf_is_dict(smask) else m.PdfObj()
    return group if m.pdf_is_stream(group) else None


def _reach(obj) -> set[int]:
    """The xrefs of the drawings, pictures, patterns and soft masks an object's resources name, inside each other
    too."""
    m = pymupdf.mupdf
    seen, todo = set(), [obj]
    while todo:
        res = m.pdf_dict_gets(todo.pop(), "Resources")
        for kind in ("XObject", "Pattern", "ExtGState"):
            names = m.pdf_dict_gets(res, kind) if m.pdf_is_dict(res) else m.PdfObj()
            for i in range(m.pdf_dict_len(names) if m.pdf_is_dict(names) else 0):
                val = m.pdf_dict_get_val(names, i)
                val = _mask(val) if kind == "ExtGState" else val
                if val is not None and m.pdf_to_num(val) and m.pdf_to_num(val) not in seen:
                    seen.add(m.pdf_to_num(val))
                    todo.append(val)
    return seen


def _without_text_in(doc, res, done: dict, depth: int, pick=None):
    """A copy of the resources ``res`` in which the drawings (form XObjects), tiling patterns and soft masks are
    copies without text (_without_text) and the property lists say no words, or None when that changes nothing.
    With ``pick``: only the patterns and soft masks whose drawing pick(drawing) takes."""
    m = pymupdf.mupdf
    if not m.pdf_is_dict(res):
        return None
    out = None
    for kind in ("Pattern", "ExtGState") if pick else ("XObject", "Pattern", "ExtGState", "Properties"):
        names = m.pdf_dict_gets(res, kind)
        mine = None
        for i in range(m.pdf_dict_len(names) if m.pdf_is_dict(names) else 0):
            new = _entry_without_text(doc, kind, m.pdf_dict_get_val(names, i), res, done, depth, pick)
            if new is not None:
                if out is None:
                    out = m.pdf_copy_dict(res)
                if mine is None:
                    mine = m.pdf_copy_dict(names)
                    m.pdf_dict_puts(out, kind, mine)
                m.pdf_dict_put(mine, m.pdf_dict_get_key(names, i), new)
    return out


def _entry_without_text(doc, kind: str, val, res, done: dict, depth: int, pick):
    """What takes the place of resource ``val`` (of ``kind``) without text, or None when it stays."""
    m = pymupdf.mupdf
    if kind == "Properties":
        if m.pdf_is_dict(val) and _says(val):
            own = val if _layer(val) else m.pdf_copy_dict(val)  # a copy would be no layer: changed in place
            for key in _ALT_KEYS:
                m.pdf_dict_dels(own, key)
            return None if own is val else own
        return None
    drawing = _mask(val) if kind == "ExtGState" else val
    if drawing is None or not m.pdf_is_stream(drawing) or not m.pdf_to_num(drawing) \
            or (kind == "Pattern" and not _tiling(drawing)) \
            or (kind == "XObject" and m.pdf_to_name(m.pdf_dict_gets(drawing, "Subtype")) != "Form") \
            or (pick and not pick(drawing)):
        return None
    xref = m.pdf_to_num(drawing)
    new = _without_text(doc, xref, res, done, depth)
    if new == xref:
        return None
    ref = m.pdf_new_indirect(pymupdf._as_pdf_document(doc), new, 0)
    if kind != "ExtGState":
        return ref
    gs, smask = m.pdf_copy_dict(val), m.pdf_copy_dict(m.pdf_dict_gets(val, "SMask"))  # the state, with a mask of
    m.pdf_dict_puts(smask, "G", ref)  # its own that draws the copy
    m.pdf_dict_puts(gs, "SMask", smask)
    return gs


def _without_text(doc, xref: int, res, done: dict, depth: int = 0) -> int:
    """The drawing ``xref`` (form XObject, tiling pattern or soft mask group) without text: a copy without the
    operators that show text and the words its marked content says, whose resources name such copies of what is
    in it; else ``xref`` itself. A drawing without resources takes ``res``, as MuPDF reads it. The rest stays byte
    for byte (MuPDF writing it out anew dropped a line width it set to the default, and lost pictures where it had
    no resources). ``done``: original -> answer."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    if xref not in done:
        done[xref] = xref  # inside itself, or this deep: the check for glyphs under the marks has the last word
        obj = m.pdf_load_object(pdf, xref)
        own = m.pdf_dict_gets(obj, "Resources")
        res = own if m.pdf_is_dict(own) else res
        data = doc.xref_stream(xref) or b""
        clean = _strip_alt(_without_shows(data, res), res)
        names = _without_text_in(doc, res, done, depth + 1) if depth < 8 else None
        if clean != data or names is not None:
            copy = m.pdf_copy_dict(obj)
            for key in ("Length", "Filter", "DecodeParms"):
                m.pdf_dict_dels(copy, key)
            if names is not None:
                m.pdf_dict_puts(copy, "Resources", names)
            done[xref] = doc.get_new_xref()
            m.pdf_update_object(pdf, done[xref], copy)
            _write(doc, done[xref], clean)
    return done[xref]


def _without_shows(data: bytes, res=None) -> bytes:
    """A content stream without its text: the operators that show text go, with their operands. A text object that
    clipped with its text (render mode 4 to 7) clips all away after it, as MuPDF's redaction does (a text object
    without text would leave the clip as it was)."""
    spans, saved, mode, clipped = [], [], 0, False
    for op, args, s, j in _operators(data, (b"q", b"Q", b"Tr", b"ET", *_SHOWS), res):
        if op == b"q":
            saved.append(mode)
        elif op == b"Q":
            mode = saved.pop() if saved else mode
        elif op == b"Tr":
            with contextlib.suppress(ValueError, IndexError):
                mode = int(float(data[args[-1][0]:args[-1][1]]))
        elif op == b"ET":
            if clipped:
                spans.append((j, j, b" 0 0 m h W n"))
            clipped = False
        else:
            spans.append((args[0][0] if args else s, j, b""))
            clipped = clipped or mode >= 4
    return _splice(data, spans) if spans else data


def _appearances(doc, xref: int) -> set[int]:
    """The streams that draw a comment or field: its /AP entries, one per state where it has states."""
    out = set()
    for kind in ("N", "D", "R"):
        how, value = doc.xref_get_key(xref, "AP/" + kind)
        if how in ("xref", "dict"):
            out |= {int(x) for x in re.findall(r"(\d+) 0 R", value)}
    return out


def _drop_inline(doc, page, hit) -> None:
    """Comments, fields and links written into /Annots itself, not as objects of their own (against the PDF
    rules), are out of PyMuPDF's reach: the ones under a mark leave the page's list, with their pop-ups."""
    m = pymupdf.mupdf
    annots = m.pdf_dict_get(m.pdf_load_object(pymupdf._as_pdf_document(doc), page.xref), m.PDF_ENUM_NAME_Annots)
    seen = page_ctm(page) * page.derotation_matrix  # PDF space -> the space annotation rects are read in
    popups = set()
    for i in reversed(range(m.pdf_array_len(annots))):
        obj = m.pdf_array_get(annots, i)
        if m.pdf_is_indirect(obj) or not m.pdf_is_dict(obj):
            continue
        if hit(pymupdf.Rect(m.pdf_to_rect(m.pdf_dict_get(obj, m.PDF_ENUM_NAME_Rect))) * seen):
            popups.add(m.pdf_to_num(m.pdf_dict_get(obj, m.PDF_ENUM_NAME_Popup)))
            m.pdf_array_delete(annots, i)
    popups.discard(0)
    for i in reversed(range(m.pdf_array_len(annots))):  # a pop-up can repeat its parent's words
        if m.pdf_to_num(m.pdf_array_get(annots, i)) in popups:
            m.pdf_array_delete(annots, i)
    for xref in popups:
        doc.update_object(xref, "<<>>")


_ALT_KEYS = ("ActualText", "Alt", "E")  # words a reader copies or reads aloud in place of the glyphs
_ALT_HINT = re.compile(rb"/(?:ActualText|Alt|E)[\s(<\[/]")
_ALT_NAMES = {b"/" + k.encode() for k in _ALT_KEYS}
_WS = b" \t\r\n\f\x00"
_REG = rb"[^\x00\t\n\f\r ()<>\[\]{}/%]"  # a byte inside a token: no white space or delimiter, as MuPDF reads them
_TOKEN_END = re.compile(rb"[\x00\t\n\f\r ()<>\[\]{}/%]")
_EOL = re.compile(rb"[\r\n]")
_IN_STRING = re.compile(rb"[\\()]")
_IN_BOX = re.compile(rb"[()<>\[\]%]")
_ESCAPED = re.compile(rb"#([0-9A-Fa-f]{2})")


def _object_end(data: bytes, i: int) -> int:
    """The index after the object or token at ``i`` (no white space there): a string, hex string, array,
    dictionary, name, number or operator. Strings nest and escape with a backslash."""
    n, c = len(data), data[i]
    if c == 0x28:  # (string)
        depth = 0
        while m := _IN_STRING.search(data, i):
            i = m.start()
            if data[i] == 0x5C:
                i += 2
                continue
            depth += 1 if data[i] == 0x28 else -1
            i += 1
            if depth == 0:
                return i
        return n
    if c == 0x5B or data.startswith(b"<<", i):  # [array] or <<dictionary>>: whatever they hold, skipped whole
        depth = 0
        while m := _IN_BOX.search(data, i):
            i = m.start()
            if data.startswith((b"<<", b">>"), i):
                depth += 1 if data[i] == 0x3C else -1
                i += 2
            elif data[i] in b"[]":
                depth += 1 if data[i] == 0x5B else -1
                i += 1
            elif data[i] in b"(<":  # a string or hex string inside
                i = _object_end(data, i)
                continue
            elif data[i] == 0x25:  # a comment, to the end of its line: MuPDF reads past what it holds
                m = _EOL.search(data, i)
                i = m.start() if m else n
                continue
            else:  # a lone ) or >
                i += 1
                continue
            if depth == 0:
                return i
        return n
    if c == 0x3C:  # <hex string>
        j = data.find(b">", i)
        return n if j < 0 else j + 1
    m = _TOKEN_END.search(data, i + 1)
    return m.start() if m else n


def _picture_end(data: bytes, j: int, head: bytes = b"", res=None) -> int:
    """After the ID of an inline picture whose dictionary (what stands between BI and ID) is ``head``: the index
    after the EI that ends its data, as MuPDF finds it. MuPDF reads the data first, as much as the size and the
    filters of the picture take, then the first EI before white space, < or /: the data can hold an EI too.
    ``res``: the resources of the stream, which can name its colour space, in the file the dictionary can point
    into. A picture MuPDF cannot read ends at the first EI between white space."""
    m = pymupdf.mupdf
    p = j + 2 if data[j:j + 2] == b"\r\n" else j + 1  # MuPDF steps over the byte after ID, CR LF as one
    try:
        own = res is not None and m.pdf_is_dict(res)
        pdf = m.pdf_get_bound_document(res) if own else m.pdf_create_document()  # for a /CS 5 0 R
        text = head + b" ID"  # text stays: MuPDF reads it where it is
        info = m.pdf_parse_dict(pdf, m.fz_open_memory(m.python_buffer_data(text), len(text)), m.PdfLexbuf(256))
        rest = m.fz_open_memory(m.python_buffer_data(memoryview(data)[p:]), len(data) - p)
        stack = None
        if own:
            stack = m.pdf_resource_stack()
            stack.resources, stack.next = res.m_internal, None  # res outlives the stack: the caller holds it
        m.ll_fz_drop_image(m.ll_pdf_load_inline_image(pdf.m_internal, stack, info.m_internal, rest.m_internal))
        p += m.fz_tell(rest)
    except Exception:  # a picture MuPDF cannot read
        k = j + 1
        while (k := data.find(b"EI", k)) >= 0 and not (data[k - 1] in _WS and
                                                        data[k + 2:k + 3] in (b"", b" ", b"\n", b"\r", b"\t")):
            k += 2
        return len(data) if k < 0 else k + 2
    while (k := data.find(b"EI", p)) >= 0:
        if k + 2 == len(data) or data[k + 2] <= 32 or data[k + 2] in b"</":
            return k + 2
        p = k + 2
    return len(data)


# white space and comments, then a token that is no string, hex string, array or dictionary, if one comes
_LEX = re.compile(rb"(?:[\x00\t\n\f\r ]+|%[^\r\n]*)*([^\x00\t\n\f\r %(<\[]" + _REG + rb"*)?")
_END, _SKIP = rb"(?!" + _REG + rb")", rb"(?:[\x00\t\n\f\r ]|%[^\r\n]*(?![^\r\n]))*"
_OPERAND = rb"(?:/" + _REG + rb"*|[+\-.0-9]" + _REG + rb"*|true|false|null)" + _END


@functools.cache
def _plain(wanted: tuple[bytes, ...]) -> re.Pattern:
    """A run of plain operations: numbers, names and keywords, then an operator not ``wanted`` (nor BI or ID). Each
    token and comment ends where it has to, so the expression never splits one and never looks back far."""
    stop = b"|".join(re.escape(w) for w in (b"true", b"false", b"null", b"BI", b"ID", *wanted))
    return re.compile(rb"(?:" + _SKIP + rb"(?:" + _OPERAND + _SKIP + rb")*(?!(?:" + stop + rb")" + _END
                      + rb")[^\x00\t\n\f\r ()<>\[\]{}/%+\-.0-9]" + _REG + rb"*" + _END + rb"){0,4096}")


def _operators(data: bytes, wanted: tuple[bytes, ...] = (b"BDC", b"Do"), res=None):
    """The operators ``wanted`` of a content stream with the spans of their operands and of themselves: (operator,
    [(start, end), ...], start, end). Strings, arrays, dictionaries and inline picture data are stepped over whole,
    comments skipped, and a run of plain operations (a drawing of many lines) goes by in one match. ``res``: the
    resources of the stream, for the size of an inline picture in a colour space named there (see _picture_end)."""
    plain = _plain(wanted)
    i, n, args, bi = 0, len(data), [], None  # bi: where the dictionary of an inline picture starts, after BI
    while True:
        if not args:
            i = plain.match(data, i).end()
        m = _LEX.match(data, i)
        s, j = m.span(1)
        if s < 0:  # the end, or a string, hex string, array or dictionary
            s = m.end()
            if s >= n:
                return
            j = _object_end(data, s)
            args.append((s, j))
        elif data[s] in b"/+-.0123456789" or data[s:j] in (b"true", b"false", b"null"):
            args.append((s, j))
        else:
            op = data[s:j]
            if op in wanted:
                yield op, args, s, j
            if op == b"BI":
                bi = j
            elif op == b"ID" and bi is not None:  # its dictionary: all after BI, as the R of 5 0 R reads as an
                j, bi = _picture_end(data, j, data[bi:s], res), None  # operator here; an ID without BI is a word
            args = []
        i = j


# MuPDF 1.28.2 writes the content of a page out anew to redact it, and gets three things wrong there (the first
# two are its bugs 709556 and 709671, fixed in MuPDF 1.28.5, which no PyMuPDF has yet):
# * a move that ends where text was placed from before in the same text object (0 0 Td, the same Tm again, as
#   overprinting has it) is left out, so the text after it follows on from the last glyph;
# * ' and " move down by a line spacing of MuPDF's own making (that of the last move it wrote), off the page;
# * a drawing (form XObject) is copied as if drawn with the default state: what it sets to the default (0 Tc,
#   100 Tz, 1 w, 0 J) is left out, and the copy takes the value from where it is drawn instead.
# Two more 1.28.5 gets right too: the room a glyph it takes out leaves (and a number at the end of a TJ) is kept
# in units of the font size and horizontal scaling in force then, but written out after a Tf or Tz that changes
# them, so the rest of the line moves (an exponent, a bullet in a smaller size); and what is left of that room
# when the text object ends goes before the first text of the next one, which moves it when only a cm places it.
_SHOWS = (b"Tj", b"TJ", b"'", b'"')
_STEADY = (b"BT", b"ET", b"Td", b"TD", b"Tm", b"T*", *_SHOWS, b"TL", b"q", b"Q", b"Do", b"Tc", b"Tw", b"Tz", b"Ts",
           b"Tf", b"gs")
# text state: the default, and a value next to it that MuPDF writes
_TEXT_STATE = {b"Tc": (0.0, b"0.000001"), b"Tw": (0.0, b"0.000001"), b"Tz": (100.0, b"100.0001"),
               b"Ts": (0.0, b"0.000001")}
_LINE_STATE = {b"w": b"1", b"J": b"0", b"j": b"0", b"M": b"10"}  # line state and its default
_STROKES = (b"S", b"s", b"b", b"b*", b"B", b"B*")


def _sets_first(data: bytes, op: bytes) -> bool:
    """Whether a drawing sets the text state ``op`` before it shows text or draws a drawing, so that it does not take
    it from where it is drawn. MuPDF copies a drawing as if drawn with the default state, and left out a 0 Tr of its
    own: the copy then took the render mode of the page, invisible after a page's own OCR text (3 Tr). The copy sets
    it first. Unlike Tc or Tz (see _steady), Tr has no value next to its default for MuPDF to write instead."""
    saved, done = [], False
    for found, *_ in _operators(data, (b"q", b"Q", op, b"Do", *_SHOWS)):
        if found == b"q":
            saved.append(done)
        elif found == b"Q":
            done = saved.pop() if saved else done
        elif found == op:
            done = True
        else:
            return done
    return False


def _decimal(x: float) -> bytes:
    """A number as PDF writes it: no exponent."""
    return (b"%.12f" % x).rstrip(b"0").rstrip(b".") or b"0"


_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _string(token: bytes) -> bytes:
    """The bytes a string token stands for, a (literal) one or a <hex> one, as MuPDF reads them: a line break in a
    literal string stands for one LF, white space in a hex string for nothing, another byte there for 0."""
    if token[:1] == b"<":
        digits = bytes(c if c in b"0123456789abcdefABCDEF" else 0x30 for c in token[1:-1] if c not in _WS)
        return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())
    out, i, n = bytearray(), 1, len(token) - (token[-1:] == b")")
    while i < n:
        c, i = token[i], i + 1
        if c == 0x0D:  # CR and CR LF read as LF
            out.append(0x0A)
            if i < n and token[i] == 0x0A:
                i += 1
            continue
        if c != 0x5C or i == n:
            out.append(c)
            continue
        c, i = token[i], i + 1
        if c in _ESCAPES:
            out.append(_ESCAPES[c])
        elif 0x30 <= c <= 0x37:  # up to three octal digits
            v = c - 0x30
            for _ in range(2):
                if i < n and 0x30 <= token[i] <= 0x37:
                    v, i = v * 8 + token[i] - 0x30, i + 1
            out.append(v & 0xFF)
        elif c == 0x0D:  # a line break after the backslash stands for nothing
            if i < n and token[i] == 0x0A:
                i += 1
        elif c != 0x0A:
            out.append(c)
    return bytes(out)


def _items(token: bytes) -> list:
    """The strings (bytes) and numbers of a TJ array token; anything else counts as 0, as MuPDF reads it."""
    out, i, n = [], 1, len(token) - (token[-1:] == b"]")
    while i < n:
        if token[i] in _WS:
            i += 1
            continue
        if token[i] == 0x25:  # a comment, to the end of its line: MuPDF reads past what it holds
            m = _EOL.search(token, i)
            i = m.start() if m else n
            continue
        j = max(_object_end(token, i), i + 1)
        part = token[i:min(j, n)]
        if part[:1] in (b"(", b"<"):
            out.append(_string(part))
        else:
            try:
                out.append(float(part))
            except ValueError:
                out.append(0.0)
        i = j
    return out


def _run(font, raw: bytes, size: float, tc: float, tw: float, th: float) -> tuple[float, float]:
    """How far showing the string ``raw`` in ``font`` (a pdf_font_desc) moves the text matrix, in text space, worked
    out as MuPDF does: each glyph by its width at the size and by the char spacing (``tc``), a one-byte code 32 by
    the word spacing (``tw``) too, across by the horizontal scaling ``th`` (Tz / 100)."""
    m = pymupdf.mupdf
    pad = memoryview(raw + b"\0")  # MuPDF reads a code up to the end of raw
    end, i, x, y = m.python_buffer_data(pad[len(raw):]), 0, 0.0, 0.0
    while i < len(raw):
        n, code = m.ll_pdf_decode_cmap(font.encoding, m.python_buffer_data(pad[i:]), end)
        cid = m.ll_pdf_lookup_cmap(font.encoding, code)
        space = tw if code == 32 and n == 1 else 0.0
        if font.wmode:
            y += (m.ll_pdf_lookup_vmtx(font, cid).w * 0.001 * size + tc if cid >= 0 else 0.0) + space
        else:
            x += ((m.ll_pdf_lookup_hmtx(font, cid).w * 0.001 * size + tc if cid >= 0 else 0.0) + space) * th
        i += n
    return x, y


def _steady(data: bytes, leading: float | None = 0.0, resets=None, copied: bool = False, fonts=None,
            res=None) -> bytes:
    """``data`` such that MuPDF 1.28.2 writes it out again with its text where and as it was (see above): ' and "
    become T* and Tj, and a move that ends where text was placed from before gets a nudge MuPDF sees, a hair (a
    few millionths of the size of the place), taken back before the next move. In a drawing MuPDF copies
    (``copied``) Tc, Tw, Tz and Ts set to the default get a value next to it. ``resets(data, span)``: the line
    state to set (in q Q) before the Do whose name operand is data[span]: MuPDF writes that out. ``leading``:
    the line spacing in force at the start, None when unknown (a drawing takes it from where it is drawn): T* then
    tells no place, and the text after it gets no nudge; the check after the redaction has the last word there.
    ``res``: the resources of ``data``, for the size of an inline picture (see _operators).
    ``fonts(kind, name)``: for b"Font" the font (a pdf_font_desc) the name operand stands for, None when unknown,
    for b"ExtGState" whether it sets a font. With it, a Tf or Tz that changes the size or the scaling after text on
    the line first sets the place the line has got to (Tm), from the widths of that text, and the next move on the
    line steps back by as much first, as it moves from where the line started. Each text object starts with its
    place set (1 0 0 1 0 0 Tm, which it has anyway), so that MuPDF drops the room left from the one before.
    shortcut: works around MuPDF 1.28.2, drop it with a PyMuPDF on MuPDF 1.28.5 or later. A drawing takes Tc, Tw
    and Tz from where it is drawn: its lines get no place set until it sets them itself."""
    edits = {}  # start -> (end, bytes): what takes the place of data[start:end]; end == start inserts

    def put(at: int, new: bytes, end: int | None = None) -> None:
        old_end, old = edits.get(at, (at, b""))
        edits[at] = (max(old_end, at if end is None else end), old + new)

    def number(span) -> float:
        return float(data[span[0]:span[1]])

    def scale(m) -> tuple[float, float]:
        """How close counts as the same place for m (a text line matrix), as floats tell: finer near the origin.
        And the size of the cells places as far out are kept in: the power of two as wide as that, 1 at least."""
        tol = max(1.0, abs(m[4]), abs(m[5])) / 2 ** 19
        return tol, 2.0 ** max(0, math.ceil(math.log2(tol)))

    def near(m) -> bool:  # m (a text line matrix) where text of the text object was placed from
        tol, size = scale(m)
        for s in {max(1.0, size / 2), size, size * 2}:  # a place that close lies in cells of the size next to it
            xs = range(math.floor((m[4] - tol) / s), math.floor((m[4] + tol) / s) + 1)
            ys = range(math.floor((m[5] - tol) / s), math.floor((m[5] + tol) / s) + 1)
            if any(max(abs(a - b) for a, b in zip(m, p)) < tol for x in xs for y in ys
                   for p in placed.get((s, x, y), ())):
                return True
        return False

    def place(m) -> None:
        s = scale(m)[1]
        placed.setdefault((s, math.floor(m[4] / s), math.floor(m[5] / s)), []).append(m)

    def back() -> bytes:  # before a move on the line: the step back from a place set to where the line started
        nonlocal tlm, shift
        sx, sy = shift
        if not (sx or sy):
            return b""
        shift = (0.0, 0.0)
        if tlm is not None:
            a, b, c, d, e, f = tlm
            tlm = [a, b, c, d, e - sx * a - sy * c, f - sx * b - sy * d]
        return b" " + _decimal(-sx) + b" " + _decimal(-sy) + b" Td "

    def font(name):  # the font a Tf names, None when unknown
        if name not in known:
            known[name] = fonts(b"Font", name) if fonts else None
        return known[name]

    def gone() -> tuple[float, float] | None:  # how far the text shown on the line moved it; None when unknown
        x = y = 0.0
        try:
            for (name, size, tc, tw, th), op, span in line:
                f = None if name is None else font(name)
                if f is None or None in (size, tc, tw, th):
                    return None
                token = data[span[0]:span[1]]
                for item in _items(token) if op == b"TJ" else [_string(token)]:
                    if isinstance(item, bytes):
                        dx, dy = _run(f, item, size, tc, tw, th)
                    else:  # a number moves back by thousandths of the size
                        dx, dy = (0.0, -item * size * 0.001) if f.wmode else (-item * size * 0.001 * th, 0.0)
                    x, y = x + dx, y + dy
        except Exception:  # a string or font MuPDF reads otherwise: the check after the redaction tells
            return None
        return x, y

    def settle(at: int, size, th) -> None:
        """Before a Tf or Tz at ``at`` that changes the size or the scaling MuPDF keeps the room of what it takes
        out in: set where the line has got to."""
        nonlocal tlm, shift, moved, undo, line
        if not (in_text and line and tlm is not None) or (size == state[1] and th == state[4]):
            return
        v = gone()
        if v is None or not (v[0] or v[1]):
            return
        a, b, c, d, e, f = tlm
        tlm = [a, b, c, d, e + v[0] * a + v[1] * c, f + v[0] * b + v[1] * d]
        put(at, b" " + b" ".join(_decimal(x) for x in tlm) + b" Tm ")
        shift, moved, undo, line = (shift[0] + v[0], shift[1] + v[1]), True, b"", []

    tl, saved, in_text, known = leading, [], False, {}
    # font name, size, Tc, Tw, horizontal scaling as MuPDF moves text by them; None where unknown
    state = [None, None, *((0.0, 0.0, 1.0) if leading is not None else (None, None, None))]
    tlm, moved, placed, undo = None, False, {}, b""  # placed: where text of this text object was placed from
    line, shift = [], (0.0, 0.0)  # the shows since the line started; how far places set moved its start
    for op, args, s, j in _operators(data, _STEADY, res):
        try:
            if op == b"q":
                saved.append((tl, list(state)))
            elif op == b"Q":
                tl, state = saved.pop() if saved else (tl, state)
            elif op == b"TL":
                tl = None
                tl = number(args[-1])
            elif op == b"Tf":
                name, size = data[slice(*args[-2])], number(args[-1])
                settle(args[0][0], size, state[4])
                state[:2] = name, size
            elif op == b"gs":
                if fonts and args and fonts(b"ExtGState", data[slice(*args[-1])]):
                    state[:2] = None, None
            elif op in _TEXT_STATE:
                k = {b"Tc": 2, b"Tw": 3, b"Tz": 4}.get(op)
                value = number(args[-1])
                if op == b"Tz":
                    settle(args[0][0], state[1], value / 100)
                if k:
                    state[k] = value / 100 if op == b"Tz" else value
                if copied and value == _TEXT_STATE[op][0]:
                    put(args[-1][0], _TEXT_STATE[op][1], args[-1][1])
            elif op == b"Do":
                reset = resets(data, args[-1]) if resets and args and not in_text else b""
                if reset:
                    put(args[-1][0], b" q " + reset + b" ")
                    put(j, b" Q ")
            elif op == b"BT":  # text shown before a move is placed from where BT puts it too
                put(j, b" 1 0 0 1 0 0 Tm ")
                in_text, tlm, moved, placed, undo = True, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], True, {}, b""
                line, shift = [], (0.0, 0.0)
            elif op == b"ET":
                in_text, tlm, undo, line, shift = False, None, b"", [], (0.0, 0.0)
            elif op in (b"Td", b"TD"):
                tx, ty = number(args[-2]), number(args[-1])
                if op == b"TD":
                    tl = -ty
                put(args[-2][0], undo + back())
                undo, moved, line = b"", True, []
                if tlm is not None:
                    a, b, c, d, e, f = tlm
                    tlm = [a, b, c, d, e + tx * a + ty * c, f + tx * b + ty * d]
            elif op == b"Tm":
                undo, moved, line, shift = b"", True, [], (0.0, 0.0)
                tlm = None
                tlm = [number(x) for x in args[-6:]] if len(args) >= 6 else None
            elif op == b"T*":
                put(s, undo + back())
                undo, moved, line = b"", True, []
                tlm = None if tlm is None or tl is None else [*tlm[:4], tlm[4] - tl * tlm[2], tlm[5] - tl * tlm[3]]
            else:  # a show
                text = args[-1][0]
                if op in (b"'", b'"'):  # T* first
                    head = b""
                    if op == b'"':
                        head = b" "
                        for (start, end), key in zip(args[-3:-1], (b"Tw", b"Tc")):
                            value = data[start:end]
                            near_value = _TEXT_STATE[key][1] if copied and float(value) == 0 else value
                            head += near_value + b" " + key + b" "
                        state[2:4] = None, None
                        state[2:4] = number(args[-2]), number(args[-3])
                    put(args[-3][0] if op == b'"' else text, undo + back() + head + b" T* ", text)
                    put(s, b"Tj", j)
                    undo, moved, line = b"", True, []
                    tlm = None if tlm is None or tl is None else [*tlm[:4], tlm[4] - tl * tlm[2], tlm[5] - tl * tlm[3]]
                if moved and tlm is not None:
                    if near(tlm):  # MuPDF would leave this move out: a nudge it cannot
                        across = math.hypot(tlm[0], tlm[1]) > 1e-9
                        vx, vy = (tlm[0], tlm[1]) if across else (tlm[2], tlm[3])
                        unit = math.hypot(vx, vy)
                        if unit > 1e-9:
                            step = max(1.0, abs(tlm[4]), abs(tlm[5])) / 2 ** 17 / unit
                            for k in range(1, 17):
                                nudged = [*tlm[:4], tlm[4] + k * step * vx, tlm[5] + k * step * vy]
                                if not near(nudged):
                                    break
                            there = _decimal(k * step)
                            put(text, b" " + there + b" 0 Td " if across else b" 0 " + there + b" Td ")
                            back_ = _decimal(-k * step)
                            undo = b" " + back_ + b" 0 Td " if across else b" 0 " + back_ + b" Td "
                            place(nudged)
                    else:
                        place(list(tlm))
                moved = False
                line.append((tuple(state), b"TJ" if op == b"TJ" else b"Tj", args[-1]))
        except (ValueError, IndexError, OverflowError):  # an operator without its numbers (or with ones out of
            # range): what follows has no known place
            tlm = None
            if op == b"Tf":
                state[:2] = None, None
            elif op in (b"Tc", b"Tw", b"Tz"):
                state[{b"Tc": 2, b"Tw": 3, b"Tz": 4}[op]] = None
    edits = {at: (end, new) for at, (end, new) in edits.items() if new or end > at}
    return _splice(data, [(at, end, new) for at, (end, new) in edits.items()]) if edits else data


def _entries(data: bytes, s: int, e: int) -> list[tuple[bytes, int, int, int]]:
    """The entries of the dictionary data[s:e] (with its << >>) as (key, key start, value start, value end)."""
    out, i, e = [], s + 2, e - 2
    while i < e:
        if data[i] in _WS:
            i += 1
            continue
        if data[i] == 0x25:
            m = _EOL.search(data, i)
            i = m.start() if m else e
            continue
        k = _object_end(data, i)
        v = k
        while v < e and (data[v] in _WS or data[v] == 0x25):  # a comment can stand before the value too
            if data[v] == 0x25:
                m = _EOL.search(data, v)
                v = m.start() if m else e
            else:
                v += 1
        end = _object_end(data, v) if v < e else v
        out.append((data[i:k], i, v, end))
        i = end
    return out


class _Trace(pymupdf.mupdf.FzDevice2):
    """What a page draws inside each marked content that says an /ActualText of our own (``tag`` + a number):
    its glyphs (``glyphs``, a count), whether it draws anything at all (``drew``) and whether that touches one
    of ``marks`` (``touch``). Without marks the glyphs are all it counts, and paths and pictures stay in C."""

    def __init__(self, tag: str, marks=None):
        super().__init__()
        names = ["fill_text", "stroke_text", "clip_text", "clip_stroke_text", "ignore_text", "begin_metatext",
                 "end_metatext"]
        if marks is not None:
            names += ["fill_path", "stroke_path", "fill_image", "fill_image_mask", "clip_image_mask", "fill_shade"]
        for name in names:
            getattr(self, "use_virtual_" + name)()
        self.tag, self.where, self.open, self.live, self.begun = tag, marks is not None, [], [], set()
        self.glyphs, self.drew, self.touch = {}, set(), set()
        self.marks = [(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5) for r in marks or ()]

    def begin_metatext(self, ctx, meta, text):
        rest = text[len(self.tag):] if text and text.startswith(self.tag) else ""
        k = int(rest) if rest.isdigit() else None
        if k is not None:
            self.begun.add(k)
            self.live.append(k)
        self.open.append(k)

    def end_metatext(self, ctx):
        if self.open and self.open.pop() is not None:
            self.live.pop()

    def _settled(self) -> bool:  # nothing open, or all that is open touches a mark already
        return self.touch.issuperset(self.live)

    def _box(self, r) -> None:
        if r.x0 > r.x1 or r.y0 > r.y1:  # draws nothing
            return
        self.drew.update(self.live)
        if any(r.x0 <= x1 and x0 <= r.x1 and r.y0 <= y1 and y0 <= r.y1 for x0, y0, x1, y1 in self.marks):
            self.touch.update(self.live)

    def _text(self, text, stroke, ctm) -> None:
        if not self.live:
            return
        n, span = 0, text.head
        while span:
            n += span.len
            span = span.next
        for k in self.live:
            self.glyphs[k] = self.glyphs.get(k, 0) + n
        if self.where and not self._settled():
            self._box(pymupdf.mupdf.ll_fz_bound_text(text, stroke, ctm))

    def fill_text(self, ctx, text, ctm, *args):
        self._text(text, None, ctm)

    def stroke_text(self, ctx, text, stroke, ctm, *args):
        self._text(text, stroke, ctm)

    def clip_text(self, ctx, text, ctm, scissor):
        self._text(text, None, ctm)

    def clip_stroke_text(self, ctx, text, stroke, ctm, scissor):
        self._text(text, stroke, ctm)

    def ignore_text(self, ctx, text, ctm):  # invisible text, as an OCR layer has
        self._text(text, None, ctm)

    def fill_path(self, ctx, path, even_odd, ctm, *args):
        if not self._settled():
            self._box(pymupdf.mupdf.ll_fz_bound_path(path, None, ctm))

    def stroke_path(self, ctx, path, stroke, ctm, *args):
        if not self._settled():
            self._box(pymupdf.mupdf.ll_fz_bound_path(path, stroke, ctm))

    def fill_image(self, ctx, image, ctm, *args):
        if not self._settled():
            unit = pymupdf.mupdf.FzRect(pymupdf.mupdf.FzRect.Fixed_UNIT)
            self._box(pymupdf.mupdf.ll_fz_transform_rect(unit.internal(), ctm))

    def fill_image_mask(self, ctx, image, ctm, *args):
        self.fill_image(ctx, image, ctm)

    def clip_image_mask(self, ctx, image, ctm, *args):
        self.fill_image(ctx, image, ctm)

    def fill_shade(self, ctx, shade, ctm, *args):
        if not self._settled():
            self._box(pymupdf.mupdf.ll_fz_bound_shade(shade, ctm))


def _trace(page, tag: str, marks=None) -> _Trace:
    """One run of the page's content through a _Trace, read as _run_all reads the page: a word MuPDF takes from a
    layer that is off counts too."""
    trace = _Trace(tag, marks)
    _run_all(page, trace)
    return trace


def _drawn_streams(doc, page):
    """The streams the page draws with: its own content and the drawings (form XObjects) that uses, inside each
    other too, as (stream xref, owner, resources, data, operand spans of each BDC in it). owner: the page or drawing
    the marked-content numbers (MCID) of the stream belong to."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    res = m.pdf_dict_get_inheritable(m.pdf_load_object(pdf, page.xref), m.PdfObj(m.PDF_ENUM_NAME_Resources))
    todo, seen = [(x, page.xref, res) for x in reversed(page.get_contents())], set()
    while todo:
        xref, owner, res = todo.pop()
        if xref in seen:
            continue
        seen.add(xref)
        data, marked = doc.xref_stream(xref) or b"", []  # a broken stream reads as none
        for op, args, *_ in _operators(data, res=res):
            if op == b"BDC":
                marked.append(args)
            elif args:
                form = _resource(res, "XObject", data, args[-1])
                if m.pdf_is_indirect(form) and m.pdf_is_stream(form) and \
                        m.pdf_to_name(m.pdf_dict_gets(form, "Subtype")) == "Form":  # MuPDF skips one without data
                    own = m.pdf_dict_gets(form, "Resources")
                    todo.append((m.pdf_to_num(form), m.pdf_to_num(form), own if m.pdf_is_dict(own) else res))
        yield xref, owner, res, data, marked


def _steady_page(doc, page) -> tuple[dict[int, bytes], set[int]]:
    """The page and the drawings it uses as _steady makes them, before MuPDF's redaction writes them out anew: the
    page gets one content stream of its own, each drawing is changed in place. Where the page or a drawing draws a
    drawing, the line state that drawing does not take from there is set to the default first, as MuPDF's copy
    has it.
    A drawing without resources of its own takes those of where it is drawn, but MuPDF's copy looks its names up in
    what MuPDF's copy of the page holds so far (it lost the blend mode of highlights, and a picture): such drawings
    get those resources lent. Returns what the changed drawings held and the drawings lent resources: they get
    their own back once MuPDF has drawn the page from copies.
    shortcut: a tiling pattern, soft mask or Type3 font without resources of its own takes them from where it is
    used too, and the page MuPDF writes keeps only the names it uses itself, so such a drawing can lose a picture
    or a font; none of the 1067 files tried has one: give the page copies of them with resources if one turns up.
    shortcut: a drawing with resources of its own that names one only where it is drawn holds (MuPDF's renderer
    looks there next, its copy does not) loses it in the copy, an inline picture its colours; none of the 1248
    drawings in those files does: lend it the missing names, as above, if one turns up."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    contents = page.get_contents()
    streams = {xref: (res, data) for xref, _, res, data, _ in _drawn_streams(doc, page)}
    forms = {xref: v for xref, v in streams.items() if xref not in contents}
    lent = set()
    for xref, (res, _) in forms.items():
        obj = m.pdf_load_object(pdf, xref)
        if m.pdf_is_dict(res) and not m.pdf_is_dict(m.pdf_dict_gets(obj, "Resources")):
            m.pdf_dict_puts(obj, "Resources", res if m.pdf_is_indirect(res) else m.pdf_deep_copy_obj(res))
            lent.add(xref)
    leans: dict[int, set[bytes]] = {}

    def leaning(xref: int) -> set[bytes]:
        """The line state a drawing strokes with before it sets it, also in the drawings inside it: what it takes
        from where it is drawn. Text strokes in the render modes that stroke."""
        if xref not in leans:
            leans[xref] = set(_LINE_STATE)  # a drawing inside itself takes it all
            res, data = forms[xref]
            out, sets, saved, mode = set(), set(), [], 0
            for op, args, *_ in _operators(data, (b"q", b"Q", b"Tr", b"Do", *_LINE_STATE, *_STROKES, *_SHOWS), res):
                if op == b"q":
                    saved.append((set(sets), mode))
                elif op == b"Q":
                    sets, mode = saved.pop() if saved else (sets, mode)
                elif op in _LINE_STATE:
                    sets.add(op)
                elif op == b"Tr":
                    try:
                        mode = int(float(data[args[-1][0]:args[-1][1]]))
                    except (ValueError, IndexError):
                        mode = 1
                elif op == b"Do":
                    inner = m.pdf_to_num(_resource(res, "XObject", data, args[-1])) if args else 0
                    out |= (leaning(inner) if inner in forms else set()) - sets
                elif op in _STROKES or mode in (1, 2, 5, 6):
                    out |= set(_LINE_STATE) - sets
            leans[xref] = out
        return leans[xref]

    def resets_in(res):
        """For a stream with resources ``res``: the line state to set where it draws a drawing."""
        def resets(data: bytes, span) -> bytes:
            form = m.pdf_to_num(_resource(res, "XObject", data, span))
            if form not in forms:
                return b""
            return b" ".join(v + b" " + k for k, v in _LINE_STATE.items() if k not in leaning(form))
        return resets

    held = []  # the fonts loaded for _steady, let go of at the end

    def fonts_in(res):
        """For a stream with resources ``res``: the font a Tf names, and whether an ExtGState a gs names sets one."""
        stack = m.pdf_resource_stack()
        stack.resources, stack.next = res.m_internal, None  # res outlives the stack: the caller holds it

        def look(kind: bytes, name: bytes):
            obj = _resource(res, kind.decode(), name, (0, len(name)))
            if kind == b"ExtGState":
                return m.pdf_is_dict(obj) and m.pdf_dict_gets(obj, "Font").m_internal is not None
            if not m.pdf_is_dict(obj) or (m.pdf_to_name(m.pdf_dict_gets(obj, "Subtype")) == "Type3"
                                          and not m.pdf_is_dict(m.pdf_dict_gets(obj, "Resources"))):
                return None  # MuPDF keeps the resources a Type3 font without its own is first loaded with
            try:
                held.append(m.ll_pdf_load_font(pdf.m_internal, stack, obj.m_internal))
            except Exception:  # a font MuPDF cannot read: no widths to go by
                return None
            return held[-1]
        return look

    olds = {}
    try:
        for xref, (res, data) in forms.items():
            new = _steady(data, None, resets_in(res), copied=True, fonts=fonts_in(res), res=res)
            if new != data:
                olds[xref] = data
                _write(doc, xref, new)
        data = b"\n".join(doc.xref_stream(x) or b"" for x in contents)
        res = streams[contents[0]][0] if contents and contents[0] in streams else m.PdfObj()
        new = _steady(data, 0.0, resets_in(res), fonts=fonts_in(res), res=res)
    finally:
        for font in held:
            m.ll_pdf_drop_font(font)
    if new != data:  # a stream of its own: the old ones can serve other pages too
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, new)
        set_page_key(page, "Contents", m.pdf_new_indirect(pdf, xref, 0))
    return olds, lent


def _alt_under(doc, page, marks: list) -> dict | None:
    """Before a redaction: tell which marked content (BDC) on the page and in the drawings it uses draws something
    that touches a mark, or draws nothing, and count its glyphs. For one run of the page each marked content
    with a number (MCID) or words of its own (/ActualText, /Alt, /E) says an /ActualText of ours, which tells
    what it draws. That /ActualText stays through the redaction: MuPDF draws the page from copies of its content
    and drawings, in which _alt_settle finds it again. Returns that plan; None, with the page as it was, when
    some marked content was not reached (hidden, or read otherwise than MuPDF reads it) or the content could not
    be read: the caller then takes out all the page says again."""
    m = pymupdf.mupdf
    tag = f"aurora-{secrets.token_hex(6)}-"
    # places: (stream xref, (start, end) of its << >>) or (None, named property list), with owner and MCID
    plan = dict(tag=tag, places=[], said={}, texts={}, original={}, edits={}, drop=[], glyphs={})
    places, said, texts, original, edits = (plan[k] for k in ("places", "said", "texts", "original", "edits"))
    lists = set()
    try:
        if not _marked(doc, page):  # nothing to plan, and a page of many paths stays fast
            return plan
        for xref, owner, res, data, marked in _drawn_streams(doc, page):
            original.setdefault(xref, data)
            for args in marked:
                if len(args) < 2:
                    continue
                s, e = args[-1]
                if data.startswith(b"<<", s):
                    found = {key: data[v:z] for key, _, v, z in _entries(data, s, e)}
                    mcid = found.get(b"/MCID", b"")
                    if mcid.isdigit() or _ALT_NAMES & found.keys():  # else it has nothing that could go
                        texts[len(places)] = found.get(b"/ActualText")
                        places.append((xref, (s, e), owner, int(mcid) if mcid.isdigit() else None))
                elif data[s] == 0x2F and data[args[0][0]:args[0][1]] != b"/OC":  # /OC names a layer, not a list
                    props = _resource(res, "Properties", data, (s, e))
                    key = m.pdf_to_num(props) or props.m_internal_value()  # one place per list, however reached
                    if not m.pdf_is_dict(props) or key in lists or _layer(props):
                        continue
                    lists.add(key)
                    text, mcid = m.pdf_dict_gets(props, "ActualText"), m.pdf_dict_gets(props, "MCID")
                    if m.pdf_is_int(mcid) or _says(props):
                        said[len(places)] = m.pdf_deep_copy_obj(text) if m.pdf_is_string(text) else None
                        places.append((None, props, owner, m.pdf_to_int(mcid) if m.pdf_is_int(mcid) else None))
        for k, (xref, where, _, _) in enumerate(places):
            if xref is None:
                m.pdf_dict_puts(where, "ActualText", m.pdf_new_text_string(tag + str(k)))
            else:
                data, (s, e) = original[xref], where
                inner = b" ".join(data[a:z] for key, a, _, z in _entries(data, s, e) if key != b"/ActualText")
                ours = b"<</ActualText(" + (tag + str(k)).encode() + b") " + inner + b">>"
                edits.setdefault(xref, []).append((s, e, ours))
        for xref, spans in edits.items():
            _write(doc, xref, _splice(original[xref], spans))
        if places:
            trace = _trace(page, tag, marks)
            if not all(k in trace.begun for k in range(len(places))):
                raise LookupError("marked content MuPDF does not reach")
            plan["glyphs"] = trace.glyphs
            plan["drop"] = [k in trace.touch or k not in trace.drew for k in range(len(places))]
    except Exception:  # content this reader does not follow: the caller takes out all the page says again
        _alt_restore(doc, plan)
        return None
    return plan


def _marked(doc, page) -> bool:
    """Whether a stream the page can draw has marked content: a look at the bytes, which never misses one."""
    return any(b"BDC" in (doc.xref_stream(x) or b"") for x in _streams(doc, page)[0])


def _layer(props) -> bool:
    """A property list that is a layer (optional content), which the document's list names by its object."""
    return pymupdf.mupdf.pdf_to_name(pymupdf.mupdf.pdf_dict_gets(props, "Type")) in ("OCG", "OCMD")


def _says(props) -> bool:
    """Whether a property list says words again (/ActualText, /Alt or /E)."""
    return any(pymupdf.mupdf.pdf_dict_gets(props, key).m_internal for key in _ALT_KEYS)


def _alt_restore(doc, plan: dict, keep=frozenset()) -> None:
    """Our /ActualText out of the originals again: each stream we changed as it was, except the ones the page
    draws now (``keep``), and each named property list with its own /ActualText back."""
    m = pymupdf.mupdf
    for xref in plan["edits"]:
        if xref not in keep:
            _write(doc, xref, plan["original"][xref])
    for k, text in plan["said"].items():
        props = plan["places"][k][1]
        if text is None:
            m.pdf_dict_dels(props, "ActualText")
        else:
            m.pdf_dict_puts(props, "ActualText", m.pdf_deep_copy_obj(text))


def _alt_settle(doc, page, plan: dict) -> dict | None:
    """After the redaction: the page now draws copies of its content and drawings, which still say our
    /ActualText. In them, marked content that touched a mark, draws nothing or lost glyphs (MuPDF takes a glyph
    whose font box touches a mark, also where its ink does not) loses its /ActualText, /Alt and /E, and the rest
    gets its own /ActualText back. The originals, which other pages and comments can draw too, are put back as
    they were. Returns, for each marked-content number (page or original drawing xref, MCID), whether its words
    went. None when the copies hold marked content with words that has none of ours (MuPDF read it, the plan
    did not: MuPDF writes it out plainly, so it shows here), lost some of ours, or could not be read: the caller
    then takes out all the page says again."""
    m = pymupdf.mupdf
    tag, places = plan["tag"], plan["places"]
    ours = re.compile(re.escape(tag.encode()) + rb"(\d+)")
    local = set()
    try:
        if not places and not _marked(doc, page):  # no marked content at all, as before the redaction
            return {}
        streams = list(_drawn_streams(doc, page))
        local = {xref for xref, *_ in streams}
        drop = list(plan["drop"])
        if places:
            after = _trace(page, tag)
            if not all(k in after.begun for k in range(len(places))):
                raise LookupError("MuPDF lost marked content of ours")
            drop = [d or after.glyphs.get(k, 0) < plan["glyphs"].get(k, 0) for k, d in enumerate(drop)]
        spans, holders = {}, []
        for xref, _, res, data, marked in streams:
            if not holders or holders[-1] is not res:
                holders.append(res)
            for args in marked:
                if len(args) < 2 or not data.startswith(b"<<", args[-1][0]):
                    continue
                s, e = args[-1]
                k = ours.search(data, s, e)
                if k is None or int(k.group(1)) >= len(places):
                    if any(key in _ALT_NAMES for key, *_ in _entries(data, s, e)):
                        raise LookupError("marked content with words the plan did not see")
                    continue
                k = int(k.group(1))
                keep = [data[a:z] for key, a, _, z in _entries(data, s, e)
                        if key != b"/ActualText" and not (drop[k] and key in _ALT_NAMES)]
                if not drop[k] and plan["texts"].get(k) is not None:
                    keep.insert(0, b"/ActualText " + plan["texts"][k])
                spans.setdefault(xref, (data, []))[1].append((s, e, b"<<" + b" ".join(keep) + b">>"))
        lists = []
        for res in holders:  # property lists named in the resources (/Span /P1 BDC): the copy gets lists of its own
            props = m.pdf_dict_gets(res, "Properties") if m.pdf_is_dict(res) else m.PdfObj()
            for i in range(m.pdf_dict_len(props) if m.pdf_is_dict(props) else 0):
                entry = m.pdf_dict_get_val(props, i)
                text = m.pdf_dict_gets(entry, "ActualText") if m.pdf_is_dict(entry) else m.PdfObj()
                said = m.pdf_to_text_string(text).encode("latin-1", "replace") if m.pdf_is_string(text) else b""
                k = ours.fullmatch(said)
                if k is None or int(k.group(1)) not in plan["said"]:
                    if m.pdf_is_dict(entry) and _says(entry):
                        raise LookupError("a property list with words the plan did not see")
                    continue
                lists.append((props, i, entry, int(k.group(1))))
        for props, i, entry, k in lists:
            own = m.pdf_copy_dict(entry)
            m.pdf_dict_dels(own, "ActualText")
            if drop[k]:
                for key in _ALT_KEYS:
                    m.pdf_dict_dels(own, key)
            elif plan["said"][k] is not None:
                m.pdf_dict_puts(own, "ActualText", m.pdf_deep_copy_obj(plan["said"][k]))
            m.pdf_dict_put(props, m.pdf_dict_get_key(props, i), own)
        _alt_restore(doc, plan, local)
        for xref, (data, edits) in spans.items():
            _write(doc, xref, _splice(data, edits))
        if _left(doc, page, tag):  # none of ours may stay, written in any way
            return None
    except Exception:  # what this reader cannot follow: the caller takes out all the page says again
        _alt_restore(doc, plan, local)
        return None
    under = {}
    for k, (_, _, owner, mcid) in enumerate(places):
        if mcid is not None:
            under[owner, mcid] = under.get((owner, mcid), False) or drop[k]
    return under


def _streams(doc, page) -> tuple[list[int], list]:
    """The content streams of a page and of the drawings its resources name, also inside each other, with the
    resources each of them uses (the page's first)."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    xrefs = [x for x in list(page.get_contents()) + [x[0] for x in doc.get_page_xobjects(page.number)]
             if doc.xref_is_stream(x)]
    holders = [m.pdf_dict_get_inheritable(m.pdf_load_object(pdf, page.xref), m.PdfObj(m.PDF_ENUM_NAME_Resources))]
    return xrefs, holders + [m.pdf_dict_gets(m.pdf_load_object(pdf, x), "Resources") for x in xrefs]


def _left(doc, page, tag: str) -> bool:
    """Whether some /ActualText of ours is still in what the page can draw, written in any way."""
    m = pymupdf.mupdf
    hint = tag.encode()
    streams, holders = _streams(doc, page)
    for xref in streams:
        data = doc.xref_stream(xref) or b""
        if hint in data or hint.hex().encode() in data.lower():
            return True
    for res in holders:
        props = m.pdf_dict_gets(res, "Properties") if m.pdf_is_dict(res) else m.PdfObj()
        for i in range(m.pdf_dict_len(props) if m.pdf_is_dict(props) else 0):
            text = m.pdf_dict_gets(m.pdf_dict_get_val(props, i), "ActualText")
            if m.pdf_is_string(text) and m.pdf_to_text_string(text).startswith(tag):
                return True
    return False


def _resource(res, kind: str, data: bytes, span: tuple[int, int]):
    """What the name operand data[span] stands for in the ``kind`` dictionary (XObject, Properties) of ``res``."""
    m = pymupdf.mupdf
    if not m.pdf_is_dict(res):
        return m.PdfObj()
    return m.pdf_dict_gets(m.pdf_dict_gets(res, kind), _name(data[span[0] + 1:span[1]]).decode("latin-1"))


def _name(raw: bytes) -> bytes:
    """A name as it reads: #xx stands for the byte xx."""
    return _ESCAPED.sub(lambda g: bytes([int(g.group(1), 16)]), raw)


def _splice(data: bytes, spans: list) -> bytes:
    """``data`` with each (start, end, new bytes) of ``spans`` put in place of what it covers."""
    out, last = [], 0
    for s, e, new, *_ in sorted(spans):
        out += [data[last:s], new]
        last = e
    out.append(data[last:])
    return b"".join(out)


def _strip_alt(data: bytes, res=None) -> bytes:
    """A content stream without the /ActualText, /Alt and /E entries of its property lists (the << >> of BDC):
    keys only, with their values. Strings, arrays and inline image data are stepped over whole (``res``: see
    _operators).
    shortcut: the page and the drawings of _drop_alt_in come without their resources: an inline picture in a colour
    space named there ends at the first EI between white space; give them theirs if one shows marked content."""
    if not (_ALT_HINT.search(data) or b"#" in data):  # a key can be written with #xx
        return data
    out, last, i, keys, bi = [], 0, 0, [], None  # keys: per open dictionary, whether a key comes next; bi: after BI
    while i < len(data):
        c = data[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # a comment runs to the end of the line
            m = _EOL.search(data, i)
            i = m.start() if m else len(data)
        elif data.startswith(b"<<", i):
            if keys:
                keys[-1] = True  # a dictionary as a value: a key comes after it
            keys.append(True)
            i += 2
        elif data.startswith(b">>", i):
            if keys:
                keys.pop()
            i += 2
        else:
            j = _object_end(data, i)
            if keys and keys[-1] and _name(data[i:j]) in _ALT_NAMES:
                while j < len(data) and data[j] in _WS:
                    j += 1
                j = _object_end(data, j) if j < len(data) and not data.startswith(b">>", j) else j
                out += [data[last:i], b" "]
                last = j
            elif keys:
                keys[-1] = not keys[-1]
            elif data[i:j] == b"BI":
                bi = j
            elif data[i:j] == b"ID" and data[j:j + 1] in _WS:
                j = _picture_end(data, j, b"" if bi is None else data[bi:i], res)
            i = j
    out.append(data[last:])
    return b"".join(out)


def _drop_alt_text(doc, page) -> None:
    """Glyphs under a mark are gone, but a tagged page can still say them in the /ActualText, /Alt or /E of its
    marked content, which copying and screen readers use: take those out of the page and the drawings it uses."""
    _drop_alt_in(doc, *_streams(doc, page))


def _drop_alt_in(doc, streams: list[int], holders: list) -> None:
    """The /ActualText, /Alt and /E of the marked content in ``streams`` and of the property lists the resources in
    ``holders`` name go."""
    m = pymupdf.mupdf
    for xref in streams:
        data = doc.xref_stream(xref)
        clean = _strip_alt(data)
        if clean != data:
            _write(doc, xref, clean)
    for res in holders:  # property lists named in the resources (/Span /P1 BDC), which other pages can name too:
        props = m.pdf_dict_gets(res, "Properties") if m.pdf_is_dict(res) else None  # the page gets its own
        if props is None or not m.pdf_is_dict(props):
            continue
        for i in range(m.pdf_dict_len(props)):
            entry = m.pdf_dict_get_val(props, i)
            if not (m.pdf_is_dict(entry) and _says(entry)):
                continue
            if _layer(entry):  # a copy would be no layer: changed in place, for every page
                own = entry
            else:
                own = m.pdf_copy_dict(entry)
                m.pdf_dict_put(props, m.pdf_dict_get_key(props, i), own)
            for key in _ALT_KEYS:
                m.pdf_dict_dels(own, key)


def _drop_structure_text(doc, pages: set[int], under: dict, unsure: set[int], objs: set[int], moved: dict,
                         pictures: set) -> None:
    """The structure tree of a tagged file says the words of its elements again (/ActualText, /Alt, /E): take them
    out of every element with marked content on a redacted page (``pages``, xrefs) that ``under`` says goes or
    does not know, or on a page in ``unsure``, or pointing at a removed comment or field or what drew it
    (``objs``), at a picture that lost pixels (``pictures``: (page, picture) xrefs) or at a whole drawing on a
    redacted page, and out of the elements around it. ``under``: (page or drawing xref, MCID) -> whether its
    drawing touched a mark.
    The tree names drawings and pictures by object, which keeps them in the file, with all that was under a mark:
    it is pointed at the copy the page draws now instead (``moved``: (page, drawing) -> copy), and away from a
    removed comment's drawing or a changed picture."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    root = m.pdf_dict_getp(m.pdf_trailer(pdf), "Root/StructTreeRoot")
    if not m.pdf_is_dict(root):
        return
    seen, traced, anywhere, drawn = set(), pages | {x for _, x in moved} | {o for o, _ in under}, {}, []
    changed = {x for _, x in pictures}
    for (_, old), new in moved.items():
        anywhere.setdefault(old, new)

    def num(obj) -> int:
        return m.pdf_to_num(obj) if m.pdf_is_indirect(obj) else 0

    def still(xref: int) -> bool:  # does some page draw it, read once when asked
        if not drawn:
            drawn.append(set().union(*(set().union(*_drawings(doc, pno)) for pno in range(len(doc)))))
        return xref in drawn[0]

    def copy(on: int, xref: int) -> int:  # what a page draws in place of drawing xref (the page unknown: no page)
        return moved.get((on, xref)) or (anywhere[xref] if xref in anywhere and not still(xref) else 0)

    def goes(owner: int, pg: int, mcid: int) -> bool:
        if pg in unsure or owner in objs:
            return True
        return pg in pages and owner in traced and under.get((owner, mcid), True)  # untraced: a comment that stays

    def clean(elem) -> None:
        for key in _ALT_KEYS:
            m.pdf_dict_dels(elem, key)

    def walk(node, pg: int, path: list) -> None:
        if num(node):
            if num(node) in seen:
                return
            seen.add(num(node))
        pg = num(m.pdf_dict_gets(node, "Pg")) or pg
        path = path + [node]
        hit, kids = False, m.pdf_dict_gets(node, "K")
        many = m.pdf_is_array(kids)
        items = [m.pdf_array_get(kids, i) for i in range(m.pdf_array_len(kids))] if many else [kids]
        out = []  # kids that point at what left the file
        for i, item in enumerate(items):
            if m.pdf_is_int(item):  # a marked-content number on pg
                hit = hit or goes(pg, pg, m.pdf_to_int(item))
                continue
            if not m.pdf_is_dict(item):
                continue
            kind = m.pdf_to_name(m.pdf_dict_gets(item, "Type"))
            on = num(m.pdf_dict_gets(item, "Pg")) or pg
            if kind == "MCR":
                stm, owner = num(m.pdf_dict_gets(item, "Stm")), num(m.pdf_dict_gets(item, "StmOwn"))
                hit = hit or goes(stm or on, on, m.pdf_to_int(m.pdf_dict_gets(item, "MCID"))) or owner in objs
                if stm in objs or owner in objs:
                    out.append(i)
                elif stm and copy(on, stm):
                    m.pdf_dict_puts(item, "Stm", m.pdf_new_indirect(pdf, copy(on, stm), 0))
            elif kind == "OBJR":
                obj = num(m.pdf_dict_gets(item, "Obj"))
                # shortcut: a whole drawing on a redacted page counts as under a mark; telling would take where
                # each drawing lands
                hit = hit or on in unsure or obj in objs or (on, obj) in pictures or (on, obj) in moved
                if (on, obj) in pictures or (obj in changed and not still(obj)):
                    out.append(i)
                elif obj and copy(on, obj):
                    m.pdf_dict_puts(item, "Obj", m.pdf_new_indirect(pdf, copy(on, obj), 0))
            else:
                walk(item, pg, path)
        for i in reversed(out):
            if many:
                m.pdf_array_delete(kids, i)
            else:
                m.pdf_dict_dels(node, "K")
        if hit:
            for elem in path:
                clean(elem)

    walk(root, 0, [])
    # elements the tree does not reach still sit in the file through the parent tree, by page or drawing
    for owner in traced | unsure | objs:
        if not 0 < owner < doc.xref_length():
            continue
        key = m.pdf_dict_gets(m.pdf_load_object(pdf, owner), "StructParents")
        if not m.pdf_is_int(key):
            continue
        entry = m.pdf_lookup_number(m.pdf_dict_gets(root, "ParentTree"), m.pdf_to_int(key))
        elems = [m.pdf_array_get(entry, i) for i in range(m.pdf_array_len(entry))] if m.pdf_is_array(entry) else [entry]
        for mcid, elem in enumerate(elems):
            if not (owner in unsure or owner in objs or under.get((owner, mcid), owner in traced)):
                continue
            for _ in range(64):  # the element and the ones around it, up to the root
                if not m.pdf_is_dict(elem) or m.pdf_to_num(elem) == num(root):
                    break
                clean(elem)
                elem = m.pdf_dict_gets(elem, "P")


def count_redactions(doc: pymupdf.Document) -> int:
    return sum(len(list(p.annots(types=[pymupdf.PDF_ANNOT_REDACT]))) for p in doc)


# --- editing existing annotations -----------------------------------------------------


def _load(page, xref: int) -> pymupdf.Annot:
    a = page.load_annot(int(xref))
    if a is None:
        raise ValueError("annotation not found")
    return a


def delete_annot(page, xref: int) -> None:
    page.delete_annot(_load(page, xref))


def _pdf_coords(page, pts) -> str:
    inv = to_pdf(page)
    out = []
    for p in pts:
        q = pymupdf.Point(p) * inv
        out.append(f"{q.x:g} {q.y:g}")
    return " ".join(out)


def move_annot(page, xref: int, dx: float, dy: float) -> None:
    """Shift an annotation by (dx, dy) display points."""
    a = _load(page, xref)
    t = a.type[0]
    if t in MARKUP_TYPES:
        raise ValueError("text markup cannot be moved")
    v = vec_to_unrot(dx, dy, page)
    doc = page.parent
    if t in VERTEX_TYPES:
        verts = a.vertices or []
        if t == pymupdf.PDF_ANNOT_INK:
            arr = "[" + " ".join("[" + _pdf_coords(page, [pymupdf.Point(p) + v for p in s]) + "]" for s in verts) + "]"
            doc.xref_set_key(a.xref, "InkList", arr)
        else:
            key = "L" if t == pymupdf.PDF_ANNOT_LINE else "Vertices"
            doc.xref_set_key(a.xref, key, "[" + _pdf_coords(page, [pymupdf.Point(p) + v for p in verts]) + "]")
        _load(page, xref).update()
        return
    # Shift the stored /Rect itself. Going through set_rect() drifts for NoRotate
    # annotations (sticky notes) on rotated pages, whose reported rect is turned about
    # a corner; a pure translation of /Rect is exact for every box-shaped type.
    inv = to_pdf(page)
    d = pymupdf.Point(v) * inv - pymupdf.Point(0, 0) * inv
    targets = [a.xref]
    popup = getattr(a, "popup_xref", 0)
    if popup:
        targets.append(popup)
    for x in targets:
        kind, val = doc.xref_get_key(x, "Rect")
        if kind != "array":
            continue
        x0, y0, x1, y1 = (float(t) for t in val.strip("[]").split())
        doc.xref_set_key(x, "Rect", f"[{x0 + d.x:g} {y0 + d.y:g} {x1 + d.x:g} {y1 + d.y:g}]")
    if doc.xref_get_key(a.xref, "AP")[0] == "null":  # a drawing follows its /Rect: redrawing would lose another program's
        _load(page, xref).update()


def resize_annot(page, xref: int, rect: Sequence[float]) -> None:
    """Give a box-shaped annotation a new display rect."""
    a = _load(page, xref)
    if a.type[0] not in RESIZABLE:
        raise ValueError("annotation cannot be resized")
    r = pymupdf.Rect(rect) * page.derotation_matrix  # set_rect() takes the space a.rect is read in
    r.normalize()
    if r.width < 4 or r.height < 4:
        raise ValueError("too small")
    if a.type[0] in BORDERED:  # set_rect takes the inner box; the border grows it back
        w = ((a.border or {}).get("width") or 0) / size_unit(page) / 2
        r = pymupdf.Rect(r.x0 + w, r.y0 + w, r.x1 - w, r.y1 - w)
    a.set_rect(r)
    if a.info.get("name") != "ImageStamp":
        a.update()


def edit_annot(page, xref: int, *, content=None, color=None, opacity=None, width=None, author=None) -> None:
    a = _load(page, xref)
    t = a.type[0]
    if content is not None:
        a.set_info(content=content)  # FreeText shows its contents; update() below redraws it
    if color is not None:
        if t == pymupdf.PDF_ANNOT_FREE_TEXT:
            a.update(text_color=rgb(color))
        else:
            fill = (a.colors or {}).get("fill")
            a.set_colors(stroke=rgb(color), fill=fill if t != pymupdf.PDF_ANNOT_LINE else rgb(color) if fill else None)
    if opacity is not None:
        a.set_opacity(float(opacity))
    if width is not None and t not in MARKUP_TYPES | {pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_TEXT}:
        a.set_border(width=float(width) * size_unit(page))
    a.set_info(modDate=pymupdf.get_pdf_now(), **({"title": author} if author is not None else {}))
    # redraw only for a change the drawing shows: update() replaces another program's drawing with MuPDF's own
    drawn = (content is not None and t == pymupdf.PDF_ANNOT_FREE_TEXT) or any(v is not None for v in (color, opacity, width))
    if drawn and a.info.get("name") != "ImageStamp":
        a.update()


# --- forms ------------------------------------------------------------------------


def _widget(page, xref: int):
    try:  # load_widget finds it without building every widget before it
        return page.load_widget(int(xref))
    except Exception:
        raise ValueError("field not found") from None


def _head(ref) -> int:
    """The number of the object that holds the value of the widget ``ref`` (a reference to it): the widget itself,
    or the parent that names the field. A loop of /Parent, or a parent that is not a dictionary, ends the walk."""
    m = pymupdf.mupdf
    for _ in range(32):
        if m.pdf_dict_gets(ref, "T").m_internal:
            break
        parent = m.pdf_dict_gets(ref, "Parent")
        if not (m.pdf_is_indirect(parent) and m.pdf_is_dict(parent)):
            break
        ref = parent
    return m.pdf_to_num(ref)


def _field_object(doc, xref: int) -> int:
    """The object that holds a widget's value: the widget itself, or the parent that names the field."""
    return _head(pymupdf.mupdf.pdf_new_indirect(pymupdf._as_pdf_document(doc), xref, 0))


def _fields_of(doc) -> dict[int, list[tuple[int, int]]]:
    """The widgets of every field, {field: [(page number, widget xref)]}, from the pages' own lists: a field's
    /Kids can miss a widget (a copied page's), and a widget's /P can be missing or name another page.
    shortcut: reads every page's list on each change of a field with several widgets, without loading the
    pages (0.1 s for 15 000 widgets); keep the map between edits if forms that big turn up."""
    pdf = pymupdf._as_pdf_document(doc)
    out = {}
    for pno in range(len(doc)):
        for x, kind, _ in doc.page_annot_xrefs(pno):
            if x and kind == pymupdf.PDF_ANNOT_WIDGET:
                out.setdefault(_head(pymupdf.mupdf.pdf_new_indirect(pdf, x, 0)), []).append((pno, x))
    return out


def _widgets_of(page, xref: int, fields: dict | None = None) -> list[tuple]:
    """Every widget of the field that widget ``xref`` on ``page`` belongs to, as (page, xref), this one first:
    a field can show on several pages, and each of them is drawn. ``fields`` is _fields_of(doc), made here
    when the caller has none and the widget is not the field itself (which has no other widgets)."""
    doc = page.parent
    field = _field_object(doc, xref)
    if field == xref:
        return [(page, xref)]
    if fields is None:
        fields = _fields_of(doc)
    others = {x: pno for pno, x in fields.get(field, ()) if x != xref}  # once, also when on two pages' lists
    return [(page, xref)] + [(page if pno == page.number else doc[pno], x) for x, pno in others.items()]


def _auto_size(doc, w, kind: str, text: str) -> float:
    """The size MuPDF gives text in a field that sizes it itself (/DA size 0), as it does with Helvetica:
    12 in lists and multi-line fields, else the height inside the border, shrunk until the line fits.
    MuPDF's other font leaves three border widths free on each side and drops the words that do not fit,
    also on a tie, hence the point to spare."""
    if kind == "list" or w.field_flags & FF_MULTILINE:
        return 12.0
    unit = size_unit(w.parent)  # the rect as on screen, the size in units of the page (/UserUnit)
    width, height = w.rect.width * unit, w.rect.height * unit
    if doc.xref_get_key(w.xref, "MK/R")[1] in ("90", "270"):
        width, height = height, width
    b = w.border_width or 1
    size, room = max(height - 4 * b, 1.0), width - 6 * b - 1
    wide = _helv().text_length(text, size)
    return size * room / wide if wide > room > 0 else size


@functools.cache
def _helv() -> pymupdf.Font:
    return pymupdf.Font("helv")


def _upright(doc, xref: int) -> None:
    """MuPDF puts a one-line field's text that is in its other font partly or wholly below the box:
    put the line where its Helvetica lines go, the baseline at (height - 0.6 × size) / 2, upright and
    scaled as MuPDF scaled it."""
    kind, ap = doc.xref_get_key(xref, "AP/N")
    m = _TOP_DOWN.match(doc.xref_stream(int(ap.split()[0]))) if kind == "xref" else None
    if m:
        y = (float(m["h"]) - 0.6 * float(m["size"]) * float(m["d"])) / 2
        doc.update_stream(int(ap.split()[0]), b"%s%sBT\n%s\n%s 0 0 %s %s %g Tm\n%sTJ\nET\nQ\nEMC\n"
                          % (m["head"], m["color"], m["font"], m["a"], m["d"], m["x"], y, m["glyphs"]))


def _mark(doc, xref: int, places: list[int]) -> None:
    """MuPDF draws a list box's options alike: put the selection colour other readers use behind the chosen
    ones (``places`` in the option list), so print, export and other readers show what is chosen. A scrolled
    list starts at option /TI, which MuPDF reads from the widget only and rounds."""
    kind, ti = doc.xref_get_key(xref, "TI")
    first = max(int(float(ti) + 0.5), 0) if kind in ("int", "float") else 0
    rows = [i - first for i in places if i >= first]
    kind, ap = doc.xref_get_key(xref, "AP/N")
    m = _LIST.search(doc.xref_stream(int(ap.split()[0]))) if kind == "xref" and rows else None
    if not m:
        return
    x, _, width, _ = map(float, m["clip"].split())
    if m["top"]:
        fs, lh = float(m["fs"]), float(m["lh"])
        lines = [float(m["top"]) - (i + 1) * lh for i in rows]
    else:
        fs = float(m["fs2"])
        lh = float(m["lh2"]) if m["lh2"] else 1.2 * fs
        lines = [float(m["h"]) - float(m["first"]) - i * lh for i in rows]
    stream = m.string
    rects = b"".join(b"%g %g %g %g re\n" % (x, y - (lh - fs) / 2 - 0.2 * fs, width, lh) for y in lines)
    doc.update_stream(int(ap.split()[0]), stream[: m.end("cut")] + b"q\n0.6 0.757 0.855 rg\n" + rects + b"f\nQ\n"
                      + stream[m.end("cut"):])


_DRAWN = ("V", "AP", "AS", "I")  # what giving a widget a value changes in it


def _update(doc, xref: int, w) -> None:
    """w.update(), which writes the whole widget back the way PyMuPDF has it (Helvetica for the form's own
    font, a border of 1 for none, no rich text, no action that is not JavaScript): afterwards everything but
    the value and its drawing is put back as it was.
    shortcut: copies the widget through MuPDF's objects (a key at a time took a third longer), check again
    when requirements.txt moves past PyMuPDF 1.28."""
    m = pymupdf.mupdf
    obj = m.pdf_load_object(pymupdf._as_pdf_document(doc), xref)  # the object update() writes to
    before = m.pdf_deep_copy_obj(obj)  # references stay references
    for name in ("BS", "MK", "AA", "A"):  # update() writes into these where they are: an object of their own,
        val = m.pdf_dict_gets(obj, name)  # maybe shared with other fields, gets a copy to write into instead
        if m.pdf_is_indirect(val):
            m.pdf_dict_puts(obj, name, m.pdf_deep_copy_obj(m.pdf_resolve_indirect(val)))
    w.update()
    for i in reversed(range(m.pdf_dict_len(obj))):  # what update() added
        key = m.pdf_dict_get_key(obj, i)
        if m.pdf_to_name(key) not in _DRAWN and not m.pdf_dict_get(before, key).m_internal:
            m.pdf_dict_del(obj, key)
    for i in range(m.pdf_dict_len(before)):  # and what it changed or took away
        key = m.pdf_dict_get_key(before, i)
        if m.pdf_to_name(key) not in _DRAWN:
            m.pdf_dict_put(obj, key, m.pdf_dict_get_val(before, i))


def _draw(page, xref: int, w, kind: str, text: str | None) -> None:
    """Give the widget ``text`` and draw it; None draws it with the value it holds (a list's choices).

    update() skips an empty value and reset() brings back the default (/DV), so an empty text goes into
    /V itself and the widget is read again. Text outside Windows-1252 (Č, Ć, Đ, Cyrillic) MuPDF draws
    in another font, at size 0 in a field that sizes its text itself and below the box in a one-line
    field: it gets the size MuPDF would have picked for the drawing only, so the field goes on sizing
    itself, and the line is moved up into the box. A list box shows all its options, so they decide.
    A widget without a box is hidden: it only gets the value.
    """
    doc = page.parent
    if w.rect.is_empty:  # update() refuses it, and there is nothing to see
        if text is not None:
            doc.xref_set_key(_field_object(doc, xref), "V", pymupdf.get_pdf_str(text))
        return
    if text:
        w.field_value = text
    elif text is not None:
        doc.xref_set_key(_field_object(doc, xref), "V", "()")
        w = _widget(page, xref)
    shown = "".join(o if isinstance(o, str) else o[-1] for o in w.choice_values or []) if kind == "list" else text
    try:
        shown.encode("cp1252")
        other = False
    except UnicodeEncodeError:
        other = True
    if other and not w.text_fontsize:
        w.text_fontsize = _auto_size(doc, w, kind, shown)  # _update puts the field's own /DA back
    _update(doc, xref, w)
    if other and kind != "list" and not w.field_flags & FF_MULTILINE:
        _upright(doc, xref)


def _drop(doc, xref: int, key: str) -> None:
    """Take ``key`` out of an object: xref_set_key(xref, key, "null") leaves "/key null" in the file."""
    m = pymupdf.mupdf
    m.pdf_dict_dels(m.pdf_load_object(pymupdf._as_pdf_document(doc), xref), key)


def _drop_xfa(doc) -> None:
    """A hybrid form keeps its values and words again in an XFA packet, which Adobe Reader shows instead of
    the fields: after a fill, reset, flattening or redaction it would show what was there before. Without
    the packet every reader shows the fields."""
    m = pymupdf.mupdf
    form = m.pdf_dict_getp(m.pdf_trailer(pymupdf._as_pdf_document(doc)), "Root/AcroForm")
    if m.pdf_is_dict(form):
        m.pdf_dict_dels(form, "XFA")


def _chosen(doc, xref: int) -> list[str]:
    """What a field holds, read afresh: a multi-select list holds several choices in an array, which
    PyMuPDF reads as ''.
    shortcut: reads it through MuPDF's objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    m = pymupdf.mupdf
    obj = m.pdf_load_object(pymupdf._as_pdf_document(doc), xref)
    v = m.pdf_dict_get_inheritable(obj, m.PdfObj(m.PDF_ENUM_NAME_V))
    if m.pdf_is_array(v):
        return [m.pdf_to_text_string(m.pdf_array_get(v, i)) for i in range(m.pdf_array_len(v))]
    text = m.pdf_field_value(obj)  # a text string, also a name or a text stream
    return [text] if text else []


def _turn(page, xref: int, state: str, fields: dict | None = None) -> None:
    """Set the check box or radio group of widget ``xref`` to ``state`` (a state name, or "Off") the way a
    viewer does: the name in the field's /V, and in every widget's /AS that state where the widget has a
    drawing for it, else Off. A widget without drawings MuPDF draws itself, every one of them with the state
    Yes: a check box shows the state everywhere, a radio group in widget ``xref`` only. update() changed the
    clicked widget only, wrote /V as text and replaced the form's own drawings.
    shortcut: works through MuPDF's objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    doc, m = page.parent, pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    field = _field_object(doc, xref)
    head = m.pdf_load_object(pdf, field)
    m.pdf_dict_puts(head, "V", m.pdf_new_name(state))
    radio = m.pdf_field_type(head) == m.PDF_WIDGET_TYPE_RADIOBUTTON
    for _, x in _widgets_of(page, xref, fields):
        obj = m.pdf_load_object(pdf, x)
        drawings = m.pdf_dict_getp(obj, "AP/N")
        if m.pdf_is_dict(drawings):
            drawn = m.pdf_dict_gets(drawings, state).m_internal
        else:
            drawn = x == xref or not radio
        m.pdf_dict_puts(obj, "AS", m.pdf_new_name(state if drawn else "Off"))
        if x != field:
            m.pdf_dict_dels(obj, "V")  # a widget's own /V (update() wrote one) would hide the field's


def set_field(page, xref: int, value) -> list[int]:
    """Give the field of widget ``xref`` a value. Returns the pages it changed: a field can show on several."""
    w = _widget(page, xref)
    if w.field_flags & FF_READONLY:
        raise ValueError("field is read-only")
    kind = _widget_kind(w)
    if kind == "text" and w.text_maxlen:
        value = str(value)[: w.text_maxlen]  # what is typed; a longer value already in the file stays
    doc = page.parent
    fields = None if _field_object(doc, xref) == xref else _fields_of(doc)  # read once, for both below
    _fill(page, xref, w, kind, value, fields)
    _drop_xfa(doc)
    return sorted({p.number for p, _ in _widgets_of(page, xref, fields)})


def _need_appearances(doc) -> tuple[int, str]:
    """Where /NeedAppearances is read and written: /AcroForm is often an object of its own, and
    xref_set_key cannot write a path through it."""
    cat = doc.pdf_catalog()
    kind, af = doc.xref_get_key(cat, "AcroForm")
    return (int(af.split()[0]), "NeedAppearances") if kind == "xref" else (cat, "AcroForm/NeedAppearances")


def settle_fields(doc: pymupdf.Document) -> None:
    """A form with NeedAppearances asks every reader to draw its fields itself. MuPDF then draws them for the
    screen only, loses Č, Ć and Đ doing it, and flattening keeps the old drawings: draw the text fields,
    dropdowns and lists once, with the value of the field, as set_field does, and switch it off, so the page,
    its print, its exports and a flattened copy all show what the fields hold. If a field cannot be drawn, it
    stays on."""
    host, key = _need_appearances(doc)
    if doc.xref_get_key(host, key)[1] != "true":
        return
    doc.xref_set_key(host, key, "false")  # first: while it is on, each redraw takes the whole page
    drawn = False
    try:
        done, failed, fields = set(), False, _fields_of(doc)
        for page in doc:
            for w in _widgets(page):
                kind, field = _widget_kind(w), _field_object(doc, w.xref)
                if kind not in ("text", "combo", "list") or field in done:
                    continue
                done.add(field)  # _fill draws every widget of the field
                try:
                    _refill(page, w, kind, field, fields)
                except Exception:
                    failed = True
        drawn = not failed
    finally:
        if not drawn:  # also when a page cannot be read: readers go on drawing the fields
            doc.xref_set_key(host, key, "true")


def _refill(page, w, kind: str, field: int, fields: dict) -> None:
    """Draw the widgets of ``field`` again with the value the field holds."""
    value = _chosen(page.parent, field)
    _fill(page, w.xref, w, kind, value if w.field_flags & FF_MULTISELECT else (value or [""])[0], fields)


def _fill(page, xref: int, w, kind: str, value, fields: dict | None = None) -> None:
    doc = page.parent
    if kind in ("check", "radio"):
        if value or kind == "check":  # a radio button goes off when another one comes on
            m = pymupdf.mupdf
            on = m.pdf_button_field_on_state(m.pdf_load_object(pymupdf._as_pdf_document(doc), xref))
            _turn(page, xref, m.pdf_to_name(on) if value else "Off", fields)
        return
    if kind not in ("combo", "list", "text"):
        raise ValueError(f"cannot fill a {kind} field")
    field, widgets = _field_object(doc, xref), _widgets_of(page, xref, fields)
    for _, x in widgets:  # the value belongs to the field: MuPDF reads and draws a widget's own instead,
        if x != field:  # and it would stay in the file
            _drop(doc, x, "V")
    if kind == "text":
        text = str(value)
        if text != (w.field_value or ""):
            _drop(doc, field, "RV")  # its rich text would go on showing the old value
        shown = "•" * len(text) if w.field_flags & FF_PASSWORD else text  # a password is never drawn readable
        for p, x in widgets:
            _draw(p, x, w if x == xref else _widget(p, x), kind, shown)
        if shown != text:
            doc.xref_set_key(field, "V", pymupdf.get_pdf_str(text))
    elif isinstance(value, list):  # several choices: PyMuPDF takes one, so /V gets them all and /I their places
        values = [str(v) for v in value]
        places = [i for i, o in enumerate(w.choice_values or []) if (o if isinstance(o, str) else o[0]) in values]
        for key, items in (("V", "".join(map(pymupdf.get_pdf_str, values))), ("I", " ".join(map(str, places)))):
            if items:
                doc.xref_set_key(field, key, f"[{items}]")
            else:
                _drop(doc, field, key)
        for p, x in widgets:  # read again, the array as "", which update() keeps
            _draw(p, x, _widget(p, x), kind, None)
            _mark(doc, x, places)
    else:
        value = str(value)
        labels = {o[0]: o[-1] for o in w.choice_values or [] if not isinstance(o, str)}
        places = [i for i, o in enumerate(w.choice_values or []) if (o if isinstance(o, str) else o[0]) == value]
        for p, x in widgets:  # MuPDF draws the text it gets: the label ("Norway")
            _draw(p, x, w if x == xref else _widget(p, x), kind, labels.get(value, value))
            if kind == "list":
                _mark(doc, x, places)
        doc.xref_set_key(field, "V", pymupdf.get_pdf_str(value))  # and the file keeps the value ("NO"),
        _drop(doc, field, "I")  # which is what the form hands on; and the places of an old choice go


def forget_passwords(doc: pymupdf.Document) -> None:
    """Empty every password field: readers should never store its value in the file (ISO 32000-1, 12.7.4.3).
    For the copy that gets written, so the open document keeps what was typed. Looks through the pages'
    objects: loading every widget costs seconds in a big form, more when it asks readers to draw it.
    shortcut: works through MuPDF's objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    for pno in range(len(doc)):
        annots = m.pdf_dict_gets(m.pdf_lookup_page_obj(pdf, pno), "Annots")
        for i in range(m.pdf_array_len(annots)):
            obj = m.pdf_array_get(annots, i)
            try:
                secret = (m.pdf_field_type(obj) == m.PDF_WIDGET_TYPE_TEXT and m.pdf_field_flags(obj) & FF_PASSWORD
                          and m.pdf_field_value(obj))
            except Exception:  # a field MuPDF cannot read (a loop of /Parent) does not stop the save
                continue
            if secret:  # its other widgets are emptied with it
                page, xref = doc[pno], m.pdf_to_num(obj)
                _drop_xfa(doc)
                try:
                    _fill(page, xref, _widget(page, xref), "text", "")
                except Exception:  # the save goes on, and the value goes at least
                    _drop(doc, _field_object(doc, xref), "V")


def has_fields(doc: pymupdf.Document) -> bool:
    return bool(doc.is_form_pdf)


def xfa_only(doc: pymupdf.Document) -> bool:
    """A dynamic XFA form: its pages and fields are in the XFA packet, which MuPDF does not draw, so only a
    "please wait" page shows. A hybrid form has its fields as well, and they show."""
    m = pymupdf.mupdf
    trailer = m.pdf_trailer(pymupdf._as_pdf_document(doc))
    if m.pdf_is_null(m.pdf_dict_getp(trailer, "Root/AcroForm/XFA")):
        return False
    return bool(m.pdf_to_bool(m.pdf_dict_getp(trailer, "Root/NeedsRendering"))
                or not m.pdf_array_len(m.pdf_dict_getp(trailer, "Root/AcroForm/Fields")))


def reset_fields(doc: pymupdf.Document) -> int:
    """Every field back to its default (/DV). A field is reset as a whole (Widget.reset() on one widget of
    several resets only that widget), and only then drawn: MuPDF's reset draws a dropdown's value ("NO"),
    loses Č, Ć, Đ and leaves a list's /I (its chosen places) as it was, so each field gets its default again
    as set_field gives it. A check box or radio group gets its default as a click gives it: MuPDF's reset
    turns only the widgets that /Kids lists and has them drawn its own way at the next render."""
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    n, reset = 0, {}
    for page in doc:
        for w in _widgets(page):
            kind = _widget_kind(w)
            if kind in ("signature", "button") or w.field_flags & FF_READONLY:
                continue
            n += 1
            field = _field_object(doc, w.xref)
            if field not in reset:
                if kind not in ("check", "radio"):
                    m.pdf_field_reset(pdf, m.pdf_load_object(pdf, field))
                reset[field] = (page, w, kind)  # the page object too: the widget needs it to be drawn
    fields = _fields_of(doc)
    for field, (page, w, kind) in reset.items():
        if kind in ("check", "radio"):
            dv = m.pdf_dict_get_inheritable(m.pdf_load_object(pdf, field), m.PdfObj(m.PDF_ENUM_NAME_DV))
            _turn(page, w.xref, m.pdf_to_name(dv) or m.pdf_to_text_string(dv) or "Off", fields)
        elif kind in ("combo", "list", "text"):
            _refill(page, w, kind, field, fields)  # the field's value: a widget /Kids does not list keeps one of its own
    if n:
        _drop_xfa(doc)
    return n


def flatten(doc: pymupdf.Document) -> None:
    """Burn form fields into the page content. Comments stay: baking would draw a redaction mark without
    taking the text under it."""
    doc.bake(annots=False, widgets=True)
    _drop_xfa(doc)  # else Adobe Reader shows the form's fields again
