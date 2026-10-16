"""Page overlay data and markup editing.

* :func:`page_info` - everything the viewer draws on top of a rendered page: the text
  layer (for selection/copy), annotations, links and form fields, all in display space.
* annotation edits - ink, shapes, text markup, free text, notes, image stamps, redaction
  marks - created as real PDF annotations so they stay editable in other readers.
* form filling and flattening.
"""
from __future__ import annotations

import functools
import re
from typing import Sequence

import pymupdf

from .geom import display_angle, hexcolor, point_to_unrot, rect_to_unrot, rgb, rounded, to_pdf, vec_to_unrot
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
    m = page.rotation_matrix
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
        "w": (a.border or {}).get("width") or 0,
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
    m = page.rotation_matrix
    for link in page.get_links():
        r = rounded(pymupdf.Rect(link["from"]) * m)
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
            "fs": w.text_fontsize or 0,
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
    out = []
    for pno, page in enumerate(doc):
        for a in page.annots():
            if a.type[0] in SKIP_TYPES:
                continue
            d = annot_info(page, a)
            d["p"] = pno
            out.append(d)
    return out


# --- creating annotations ----------------------------------------------------------


def _finish(annot: pymupdf.Annot, author: str, *, stroke=None, fill=None, width=None,
            opacity=None, content=None, update: bool = True) -> int:
    if stroke is not None or fill is not None:
        annot.set_colors(stroke=stroke, fill=fill)
    if width is not None:
        annot.set_border(width=width)
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
        r, text, fontsize=fontsize, fontname="helv", text_color=rgb(color),
        fill_color=rgb(fill) if fill else None, rotate=page.rotation,
    )
    return _finish(a, author)


def add_note(page, point: Sequence[float], text: str, *, color="#facc15", author="") -> int:
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
        for r in page.search_for(query):
            a = page.add_redact_annot(r, fill=(0, 0, 0))
            _finish(a, author)
            n += 1
    return n


def apply_redactions(doc: pymupdf.Document) -> int:
    """Burn in every redaction mark: content under it is removed for good.

    MuPDF removes text, image pixels and line art. Comments, form fields and links that
    touch a mark go too, because they can hold the same words (a highlight stores them).
    A field whose last widget goes loses its value, which its parent object keeps, a radio
    or check box group whose chosen button goes loses that choice, and a hybrid form loses
    its XFA packet, which holds every value and word of the form again.
    """
    n, heads = 0, set()
    for page in doc:
        marks = [pymupdf.Rect(a.rect) for a in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])]
        if not marks:
            continue
        n += len(marks)

        def hit(r) -> bool:
            return any(pymupdf.Rect(r).intersects(m) for m in marks)

        for xref in [a.xref for a in page.annots() if a.type[0] != pymupdf.PDF_ANNOT_REDACT and hit(a.rect)]:
            page.delete_annot(page.load_annot(xref))
        widgets = [w.xref for w in _widgets(page) if hit(w.rect)]
        heads |= {_field_object(doc, x) for x in widgets} - set(widgets)
        for xref in widgets:
            page.delete_widget(page.load_widget(xref))
        for link in [lk for lk in page.get_links() if hit(lk["from"])]:
            page.delete_link(link)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=1, text=0)
    if heads:
        fields = _fields_of(doc)
        for field in heads:
            if field not in fields:  # no widget of it is left on any page
                for key in ("V", "DV", "RV", "I"):
                    _drop(doc, field, key)
                continue
            # a radio or check box group names the chosen button: a state no button left can show was redacted
            states = {s for _, w in fields[field] for s in _states(doc, w)}
            for key in ("V", "DV"):
                kind, value = doc.xref_get_key(field, key)
                if kind == "name" and value != "/Off" and value[1:] not in states:
                    _drop(doc, field, key)
    if n:
        _drop_xfa(doc)
    return n


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
    a = _load(page, xref)
    if a.info.get("name") != "ImageStamp":
        a.update()


def resize_annot(page, xref: int, rect: Sequence[float]) -> None:
    """Give a box-shaped annotation a new display rect."""
    a = _load(page, xref)
    if a.type[0] not in RESIZABLE:
        raise ValueError("annotation cannot be resized")
    r = rect_to_unrot(rect, page)
    if r.width < 4 or r.height < 4:
        raise ValueError("too small")
    if a.type[0] in BORDERED:  # set_rect takes the inner box; the border grows it back
        w = ((a.border or {}).get("width") or 0) / 2
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
        a.set_border(width=float(width))
    a.set_info(modDate=pymupdf.get_pdf_now(), **({"title": author} if author is not None else {}))
    if a.info.get("name") != "ImageStamp":
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
    width, height = w.rect.width, w.rect.height
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


def _states(doc, xref: int) -> set[str]:
    """The appearance states of a button widget (its /AP /N names), e.g. {"Off", "Yes"}."""
    m = pymupdf.mupdf
    faces = m.pdf_dict_getp(m.pdf_load_object(pymupdf._as_pdf_document(doc), xref), "AP/N")
    if not m.pdf_is_dict(faces) or m.pdf_is_stream(faces):
        return set()
    return {m.pdf_to_name(m.pdf_dict_get_key(faces, i)) for i in range(m.pdf_dict_len(faces))}


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
                    value = _chosen(doc, field)
                    _fill(page, w.xref, w, kind,
                          value if w.field_flags & FF_MULTISELECT else (value or [""])[0], fields)
                except Exception:
                    failed = True
        drawn = not failed
    finally:
        if not drawn:  # also when a page cannot be read: readers go on drawing the fields
            doc.xref_set_key(host, key, "true")


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
            value = _chosen(doc, field)  # the field's: a widget /Kids does not list keeps a value of its own
            _fill(page, w.xref, w, kind, value if w.field_flags & FF_MULTISELECT else (value or [""])[0], fields)
    if n:
        _drop_xfa(doc)
    return n


def flatten(doc: pymupdf.Document, *, annots: bool = False) -> None:
    """Burn form fields (and optionally all annotations) into the page content."""
    doc.bake(annots=annots, widgets=True)
    _drop_xfa(doc)  # else Adobe Reader shows the form's fields again
