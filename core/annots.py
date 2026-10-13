"""Page overlay data and markup editing.

* :func:`page_info` - everything the viewer draws on top of a rendered page: the text
  layer (for selection/copy), annotations, links and form fields, all in display space.
* annotation edits - ink, shapes, text markup, free text, notes, image stamps, redaction
  marks - created as real PDF annotations so they stay editable in other readers.
* form filling and flattening.
"""
from __future__ import annotations

from typing import Sequence

import pymupdf

from .geom import display_angle, hexcolor, point_to_unrot, rect_to_unrot, rgb, rounded, vec_to_unrot
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
    out = []
    m = page.rotation_matrix
    for link in page.get_links():
        r = rounded(pymupdf.Rect(link["from"]) * m)
        kind = link.get("kind")
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and link.get("page", -1) >= 0:
            out.append({"r": r, "p": link["page"]})
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


def page_fields(page: pymupdf.Page) -> list[dict]:
    out = []
    m = page.rotation_matrix
    for w in page.widgets():
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
        if kind in ("check", "radio"):
            d["on"] = w.on_state()
            d["v"] = w.field_value not in (None, False, "Off", "")
            if kind == "radio":
                d["v"] = str(w.field_value) == str(w.on_state())
        elif kind in ("combo", "list"):
            d["opts"] = [o if isinstance(o, str) else (o[1] if len(o) > 1 else o[0]) for o in (w.choice_values or [])]
            d["multi"] = bool(flags & FF_MULTISELECT)
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
    "squiggly": "add_squiggly_annot",
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
    default = {"highlight": "#facc15", "underline": "#16a34a", "strikeout": "#dc2626", "squiggly": "#7c3aed"}[kind]
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
    """
    n = 0
    for page in doc:
        marks = [pymupdf.Rect(a.rect) for a in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])]
        if not marks:
            continue
        n += len(marks)

        def hit(r) -> bool:
            return any(pymupdf.Rect(r).intersects(m) for m in marks)

        for xref in [a.xref for a in page.annots() if a.type[0] != pymupdf.PDF_ANNOT_REDACT and hit(a.rect)]:
            page.delete_annot(page.load_annot(xref))
        for xref in [w.xref for w in page.widgets() if hit(w.rect)]:
            page.delete_widget(page.load_widget(xref))
        for link in [lk for lk in page.get_links() if hit(lk["from"])]:
            page.delete_link(link)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=1, text=0)
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
    inv = ~page.transformation_matrix
    out = []
    for p in pts:
        q = pymupdf.Point(p) * inv
        out.append(f"{q.x:g} {q.y:g}")
    return " ".join(out)


def _inner_rect(a: pymupdf.Annot) -> pymupdf.Rect:
    """Rect to hand back to set_rect so the border does not grow the box on every edit."""
    r = pymupdf.Rect(a.rect)
    if a.type[0] in BORDERED:
        w = ((a.border or {}).get("width") or 0) / 2
        r = pymupdf.Rect(r.x0 + w, r.y0 + w, r.x1 - w, r.y1 - w)
    return r


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
    inv = ~page.transformation_matrix
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
    for w in page.widgets():
        if w.xref == int(xref):
            return w
    raise ValueError("field not found")


def set_field(page, xref: int, value) -> None:
    w = _widget(page, xref)
    if w.field_flags & FF_READONLY:
        raise ValueError("field is read-only")
    kind = _widget_kind(w)
    if kind == "check":
        w.field_value = w.on_state() if value else "Off"
    elif kind == "radio":
        if not value:
            return
        w.field_value = w.on_state()
    elif kind in ("combo", "list"):
        w.field_value = value if isinstance(value, list) else str(value)
    elif kind == "text":
        text = str(value)
        if w.text_maxlen:
            text = text[: w.text_maxlen]
        if not text:
            w.reset()  # update() ignores an empty value; reset() really clears the field
            return
        w.field_value = text
    else:
        raise ValueError(f"cannot fill a {kind} field")
    w.update()


def has_fields(doc: pymupdf.Document) -> bool:
    return bool(doc.is_form_pdf)


def reset_fields(doc: pymupdf.Document) -> int:
    n = 0
    for page in doc:
        for w in page.widgets():
            kind = _widget_kind(w)
            if kind in ("signature", "button") or w.field_flags & FF_READONLY:
                continue
            w.reset()
            n += 1
    return n


def flatten(doc: pymupdf.Document, *, annots: bool = False) -> None:
    """Burn form fields (and optionally all annotations) into the page content."""
    doc.bake(annots=annots, widgets=True)
