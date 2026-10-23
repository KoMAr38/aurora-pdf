"""Aurora PDF - document-level engine on top of PyMuPDF.

Stateless functions over an open ``pymupdf.Document`` (or file paths for producers that
write new files). The session layer (core.session) owns the open document, undo history
and locking. Coordinates coming from the UI are in display space - see core.geom.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import logging
import os
import re
from functools import lru_cache
from typing import Callable, Iterable, Sequence

import pymupdf

from .geom import page_ctm, rgb, rounded, set_page_key, to_insert, to_text, turn
from .paths import FONT_SANS, write_atomic

log = logging.getLogger("aurora.pdftools")

FONT_NAME = "AuroraSans"  # resource name for the bundled Unicode font (Latin, Cyrillic, Greek)

# --- encryption / permissions -------------------------------------------------

ENCRYPTION = {
    "aes256": pymupdf.PDF_ENCRYPT_AES_256,
    "aes128": pymupdf.PDF_ENCRYPT_AES_128,
    "rc4": pymupdf.PDF_ENCRYPT_RC4_128,
}

# user-facing permission name -> PyMuPDF flag. A permission is GRANTED when its bit is set.
_PERM_FLAGS = {
    "print": pymupdf.PDF_PERM_PRINT,
    "print_hq": pymupdf.PDF_PERM_PRINT_HQ,
    "copy": pymupdf.PDF_PERM_COPY,
    "modify": pymupdf.PDF_PERM_MODIFY,
    "annotate": pymupdf.PDF_PERM_ANNOTATE,
    "form": pymupdf.PDF_PERM_FORM,
    "assemble": pymupdf.PDF_PERM_ASSEMBLE,
    "accessibility": pymupdf.PDF_PERM_ACCESSIBILITY,
}
ALL_PERMS = tuple(_PERM_FLAGS)


def permissions_int(allowed: Iterable[str]) -> int:
    """Combine granted permission names into the PDF /P integer."""
    value = 0
    for name in allowed:
        flag = _PERM_FLAGS.get(name)
        if flag is None:
            raise ValueError(f"unknown permission: {name}")
        value |= flag
    return value


def _encrypt_dict(doc: pymupdf.Document) -> str | None:
    """The trailer /Encrypt dictionary as PDF source, or None. Readable before authentication."""
    kind, val = doc.xref_get_key(-1, "Encrypt")
    if kind == "dict":
        return val
    if kind == "xref":
        return doc.xref_object(int(val.split()[0]))
    return None


def _algorithm(enc: str) -> str:
    v = re.search(r"/V\s+(\d+)", enc)
    if v and int(v.group(1)) >= 5:
        return "aes256"
    if "AESV2" in enc:
        return "aes128"
    return "rc4"


# --- open / save --------------------------------------------------------------


def is_user_password(path: str, password: str) -> bool:
    """True when ``password`` is the open (user) password of the file at ``path``."""
    doc = pymupdf.open(path)
    try:
        return bool(doc.authenticate(password) & 2)  # 2 = user password, 4 = owner, 6 = both
    finally:
        doc.close()


def free_path(path: str) -> str:
    """``path``, or ``name (2).ext``, ``name (3).ext``... if it exists: exports never overwrite a file."""
    base, ext = os.path.splitext(path)
    k = 1
    while os.path.exists(path):
        k += 1
        path = f"{base} ({k}){ext}"
    return path


def load_for_editing(path: str, password: str | None = None) -> tuple[pymupdf.Document, dict]:
    """Open ``path`` as an in-memory, unencrypted working copy plus what we learned about it.

    Working on a plain copy keeps MuPDF's encrypted-document quirks out of the edit session
    and holds no file handle, so the original can be replaced on save (Windows locks open
    files). Protection is re-applied at save time from the returned ``info``.

    Non-PDF inputs PyMuPDF understands (images, XPS, EPUB...) are converted to PDF.
    Raises ValueError("NEED_PASSWORD" | "WRONG_PASSWORD" | "NOT_SUPPORTED").
    """
    try:
        src = pymupdf.open(path)
    except Exception as exc:  # unreadable / unknown format
        raise ValueError("NOT_SUPPORTED") from exc
    try:
        if not src.is_pdf:
            try:
                data = src.convert_to_pdf()
            except Exception as exc:  # a damaged picture opens, and fails only here
                raise ValueError("NOT_SUPPORTED") from exc
            return pymupdf.open("pdf", data), {"protected": False, "converted": True}
        needs = bool(src.needs_pass)
        enc = _encrypt_dict(src)
        protected = needs or enc is not None
        auth = 0
        if needs and not password:
            raise ValueError("NEED_PASSWORD")
        if protected and password:
            auth = src.authenticate(password)
            if not auth:
                raise ValueError("WRONG_PASSWORD")
        elif protected:
            # no password typed: 1 = no passwords at all (full rights), 2 = user-level access
            auth = src.authenticate("") or 2
        info: dict = {"protected": protected, "converted": False}
        if protected:
            m = re.search(r"/P\s+(-?\d+)", enc or "")
            perms = int(m.group(1)) if m else -1
            owner = auth in (1, 4, 6)
            info.update(
                algorithm=_algorithm(enc or ""),
                perms=perms,
                owner=owner,
                # passwords we know for sure; None = unknown, "" = none set
                user_pw=(password if auth in (2, 6) and needs else ("" if not needs else None)),
                owner_pw=(password if auth in (4, 6) else ("" if auth == 1 else None)),
            )
        plain = pymupdf.open("pdf", src.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        return plain, info
    finally:
        src.close()


def rights_for(info: dict) -> dict:
    """What the user may do with a document opened via :func:`load_for_editing`."""
    full = {k: True for k in ("modify", "annotate", "form", "assemble", "copy", "print")}
    if not info.get("protected") or info.get("owner"):
        return full
    p = info.get("perms", -1)
    return {
        "modify": bool(p & pymupdf.PDF_PERM_MODIFY),
        "annotate": bool(p & pymupdf.PDF_PERM_ANNOTATE),
        "form": bool(p & (pymupdf.PDF_PERM_FORM | pymupdf.PDF_PERM_ANNOTATE)),
        "assemble": bool(p & (pymupdf.PDF_PERM_ASSEMBLE | pymupdf.PDF_PERM_MODIFY)),
        "copy": bool(p & pymupdf.PDF_PERM_COPY),
        "print": bool(p & pymupdf.PDF_PERM_PRINT),
    }


def _working_copy(doc: pymupdf.Document, subset_fonts: bool = False) -> pymupdf.Document:
    """The copy every saved file is written from."""
    from . import annots  # imports this module

    tmp = pymupdf.open("pdf", doc.tobytes())
    annots.forget_passwords(tmp)
    if subset_fonts:
        try:
            tmp.subset_fonts()
        except Exception:  # shortcut: subsetting only saves space; a full font is still correct
            pass
    return tmp


def clean_bytes(doc: pymupdf.Document, subset_fonts: bool = False) -> bytes:
    """Unencrypted, garbage-collected bytes of ``doc`` (the live document is not touched)."""
    tmp = _working_copy(doc, subset_fonts)
    try:
        return tmp.tobytes(garbage=1, deflate=True)
    finally:
        tmp.close()


def save_pdf(
    doc: pymupdf.Document,
    out_path: str,
    *,
    user_pw: str | None = None,
    owner_pw: str | None = None,
    allowed: Sequence[str] | None = None,
    perms: int | None = None,
    algorithm: str = "aes256",
    compress: bool = False,
    subset_fonts: bool = False,
) -> None:
    """Write ``doc`` to ``out_path`` without touching the in-memory document.

    The write goes through a throw-away copy: garbage collection renumbers objects, and
    the live session must keep stable xrefs (the UI addresses annotations by xref).
    A password turns encryption on; ``allowed`` (names) or ``perms`` (raw int) set the
    granted permissions, defaulting to all. No password = no encryption.
    """
    tmp = _working_copy(doc, subset_fonts)
    try:
        opts: dict = dict(garbage=1, deflate=True)
        if compress:  # not clean=True: MuPDF 1.28.2 writes the content anew then, and moved text (see annots._steady)
            opts.update(garbage=4, deflate_images=True, deflate_fonts=True, use_objstms=1)
        if user_pw or owner_pw:
            if perms is None:
                perms = permissions_int(allowed if allowed is not None else ALL_PERMS)
            opts.update(
                encryption=ENCRYPTION.get(algorithm, pymupdf.PDF_ENCRYPT_AES_256),
                owner_pw=owner_pw or user_pw or "",
                user_pw=user_pw or "",
                permissions=perms,
            )
        else:
            opts["encryption"] = pymupdf.PDF_ENCRYPT_NONE
        write_atomic(out_path, lambda path: tmp.save(path, **opts))
    finally:
        tmp.close()


def protection_args(protection: dict | None) -> dict:
    """:func:`save_pdf` keywords for a protection as the session keeps it ({user_pw, owner_pw, perms,
    algorithm}); None or {} means none."""
    p = protection or {}
    perms = p.get("perms")
    return {"user_pw": p.get("user_pw"), "owner_pw": p.get("owner_pw"), "algorithm": p.get("algorithm") or "aes256",
            "perms": None if perms in (None, -1) else perms}


# --- page ranges --------------------------------------------------------------


def parse_page_range(spec: str, total: int) -> list[int]:
    """Parse ``"1-3,5,8-"`` (1-based, inclusive) into sorted 0-based indices.

    Empty/``"all"`` means every page. An open or too long end is clamped ("8-", "2-99999"); a part that starts
    past the last page, or an empty result, raises ValueError, so "3,50" is never taken for page 3 alone.
    """
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "*", "sve", "alle"):
        return list(range(total))
    out: set[int] = set()
    for chunk in spec.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else total
        else:
            start = end = int(chunk)
        if start > total:
            raise ValueError(f"page {start} of {total}")
        out.update(range(max(start, 1) - 1, min(end, total)))  # "2-99999999" must not count to 99999999
    if not out:
        raise ValueError("empty page range")
    return sorted(out)


# --- Unicode text helpers --------------------------------------------------------


@lru_cache(maxsize=1)
def _font() -> pymupdf.Font:
    return pymupdf.Font(fontfile=str(FONT_SANS))


def text_length(text: str, fontsize: float) -> float:
    return _font().text_length(text, fontsize=fontsize)


def _insert_unicode_text(page: pymupdf.Page, point, text: str, **kw) -> None:
    page.insert_text(point, text, fontname=FONT_NAME, fontfile=str(FONT_SANS), **kw)


@lru_cache(maxsize=1)
def _outlines():
    from fontTools.ttLib import TTFont  # comes with pdf2docx

    font = TTFont(str(FONT_SANS))
    return font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm


def _draw_text_outlines(page: pymupdf.Page, mat: pymupdf.Matrix, text: str, fontsize: float, **finish) -> None:
    """Draw ``text`` as filled glyph shapes; ``mat`` takes a point (x right, y down, baseline at y=0) to the space
    page drawings use. A watermark drawn this way is no text at all, so no reader can select, copy or search it,
    whatever the permissions say."""
    from fontTools.pens.basePen import BasePen

    glyphs, cmap, upem = _outlines()
    s = fontsize / upem
    shape = page.new_shape()
    x = 0.0

    class Pen(BasePen):
        def pt(self, p):
            return pymupdf.Point(x + p[0] * s, -p[1] * s) * mat

        def _moveTo(self, p):
            self.first = self.last = self.pt(p)

        def _lineTo(self, p):
            q = self.pt(p)
            shape.draw_line(self.last, q)
            self.last = q

        def _curveToOne(self, p1, p2, p3):
            q = self.pt(p3)
            shape.draw_bezier(self.last, self.pt(p1), self.pt(p2), q)
            self.last = q

        def _qCurveToOne(self, p1, p2):  # quadratic -> cubic
            c, q = self.pt(p1), self.pt(p2)
            shape.draw_bezier(self.last, self.last + (c - self.last) * (2 / 3), q + (c - q) * (2 / 3), q)
            self.last = q

        def _closePath(self):
            if self.last != self.first:
                shape.draw_line(self.last, self.first)

    for ch in text:
        name = cmap.get(ord(ch), ".notdef")
        glyphs[name].draw(Pen(glyphs))
        x += glyphs[name].width * s
    overlay = finish.pop("overlay", True)
    shape.finish(color=None, closePath=False, **finish)
    shape.commit(overlay=overlay)


# --- watermark / header & footer ---------------------------------------------


def watermark_text(
    doc: pymupdf.Document,
    text: str,
    *,
    pages: Sequence[int] | None = None,
    opacity: float = 0.15,
    rotate: int = 45,
    fontsize: int = 48,
    color: str = "#8892a6",
    tile: bool = False,
    behind: bool = False,
) -> None:
    """Stamp ``text`` across the chosen pages, upright on screen whatever the page /Rotate.

    ``rotate`` is the visual counter-clockwise angle. Positions are planned in display
    space and converted, so rotated pages get the same look as normal ones.
    """
    if not text.strip():
        raise ValueError("empty watermark text")
    color_rgb = rgb(color)
    for pno in (range(len(doc)) if pages is None else pages):
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        if tile:
            step_x = max(text_length(text, fontsize) * 1.25, fontsize * 4)
            step_y = max(fontsize * 4.5, 140)
            centers = []
            row = 0
            y = step_y / 2
            while y < h + step_y / 2:
                x = (step_x / 2) * (row % 2)
                while x < w + step_x / 2:
                    centers.append((x, y))
                    x += step_x
                y += step_y
                row += 1
        else:
            centers = [(w / 2, h / 2)]
        for c in centers:
            _stamp_text(page, c, text, fontsize, color_rgb, opacity, rotate, behind)


def _stamp_text(page, center, text, fontsize, color_rgb, opacity, angle, behind) -> None:
    # planned on screen (y down, so a visual counter-clockwise turn is a negative angle), then into the space
    # drawings take, which is insert_image's
    mat = (pymupdf.Matrix(1, 0, 0, 1, -text_length(text, fontsize) / 2, fontsize * 0.35) * pymupdf.Matrix(-angle)
           * pymupdf.Matrix(1, 0, 0, 1, *center) * to_insert(page))
    _draw_text_outlines(page, mat, text, fontsize, fill=color_rgb, fill_opacity=opacity, overlay=not behind)


def _prepare_png(image: str | bytes, opacity: float = 1.0, rotate: int = 0) -> tuple[bytes, int, int]:
    """Load an image as RGBA PNG bytes, optionally fading it and pre-rotating it CCW."""
    from PIL import Image

    img = Image.open(io.BytesIO(image) if isinstance(image, (bytes, bytearray)) else image)
    img = img.convert("RGBA")
    if opacity < 1.0:
        img.putalpha(img.getchannel("A").point(lambda a: int(a * opacity)))
    if rotate % 360:
        img = img.rotate(rotate % 360, expand=True)  # PIL rotates counter-clockwise
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.width, img.height


def watermark_image(
    doc: pymupdf.Document,
    image: str | bytes,
    *,
    pages: Sequence[int] | None = None,
    opacity: float = 0.15,
    scale: float = 0.5,
    behind: bool = False,
) -> None:
    """Stamp an image centred on each target page at ``scale`` of the page width (on screen)."""
    cache: dict[int, tuple[bytes, int, int]] = {}
    for pno in (range(len(doc)) if pages is None else pages):
        page = doc[pno]
        rot = page.rotation
        if rot not in cache:
            # pre-rotate so the page's own /Rotate brings the image back upright
            cache[rot] = _prepare_png(image, opacity, rotate=rot)
        png, iw, ih = cache[rot]
        if rot in (90, 270):
            iw, ih = ih, iw  # on-screen proportions
        w = page.rect.width * scale
        hh = w * ih / iw
        cx, cy = page.rect.width / 2, page.rect.height / 2
        box = pymupdf.Rect(cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2) * to_insert(page)
        page.insert_image(box, stream=png, overlay=not behind, keep_proportion=True)


HF_POSITIONS = ("tl", "tc", "tr", "bl", "bc", "br")


def add_header_footer(
    doc: pymupdf.Document,
    *,
    text: str = "{n} / {total}",
    pages: Sequence[int] | None = None,
    position: str = "bc",
    fontsize: float = 10,
    color: str = "#444444",
    margin: float = 28,
    start: int = 1,
    file_name: str = "",
    date_text: str = "",
) -> None:
    """Write a header/footer line on each target page, upright on screen.

    ``text`` may use {n}, {total}, {date} and {file}; numbering runs over the
    targeted pages starting at ``start``. {date} is ``date_text`` (the UI sends today's
    date in its language), or an ISO date. Other braces stay as typed ("{Fortrolig}").
    """
    if position not in HF_POSITIONS:
        raise ValueError(f"bad position: {position}")
    targets = list(range(len(doc)) if pages is None else pages)
    color_rgb = rgb(color)
    today = date_text or datetime.date.today().isoformat()
    for k, pno in enumerate(targets):
        page = doc[pno]
        values = {"n": start + k, "total": start + len(targets) - 1, "date": today, "file": file_name}
        label = re.sub(r"\{(n|total|date|file)\}", lambda m: str(values[m[1]]), text)
        tl = text_length(label, fontsize)
        w, h = page.rect.width, page.rect.height
        x = {"l": margin, "c": (w - tl) / 2, "r": w - margin - tl}[position[1]]
        y = margin + fontsize * 0.8 if position[0] == "t" else h - margin
        m, unit = to_text(page)
        _insert_unicode_text(
            page, pymupdf.Point(x, y) * m, label,
            fontsize=fontsize * unit, color=color_rgb, rotate=page.rotation,
        )


# --- page operations (in place) ----------------------------------------------


def rotate_pages(doc: pymupdf.Document, pages: Sequence[int], degrees: int) -> None:
    for pno in pages:
        page = doc[pno]
        page.set_rotation((page.rotation + degrees) % 360)


def delete_pages(doc: pymupdf.Document, pages: Sequence[int]) -> None:
    if len(set(pages)) >= len(doc):
        raise ValueError("cannot delete every page")
    doc.delete_pages(sorted(set(pages)))


def pages_linking_to(doc: pymupdf.Document, targets: set[int]) -> set[int]:
    """The other pages with a link to one of ``targets``: deleting the targets takes those links away too."""
    out = set()
    for i in range(len(doc)):
        if i not in targets and any(kind == pymupdf.PDF_ANNOT_LINK for _, kind, _ in doc.page_annot_xrefs(i)):
            if any(link.get("page") in targets for link in doc[i].get_links()):
                out.add(i)
    return out


def reorder_pages(doc: pymupdf.Document, new_order: Sequence[int]) -> None:
    if sorted(new_order) != list(range(len(doc))):
        raise ValueError("new_order must be a permutation of all page indices")
    doc.select(list(new_order))


def insert_blank(doc: pymupdf.Document, at: int, width: float = 595, height: float = 842) -> int:
    """Insert an empty page before index ``at`` (len(doc) appends). Returns its index."""
    at = max(0, min(at, len(doc)))
    doc.new_page(pno=at, width=width, height=height)
    return at


def load_source(path: str, password: str | None = None) -> pymupdf.Document:
    """Open another file whose pages go into this or a new document (insert, merge).

    Its protection does not come along, so a protected file is only taken with its owner password (the
    same rule as for saving without protection) or when it grants every permission, so nothing is lost.
    Raises ValueError("SOURCE_PROTECTED") otherwise, and "WRONG_PASSWORD" for a password that does not
    open it.
    """
    try:
        doc, info = load_for_editing(path, password)
    except ValueError as exc:
        if str(exc) == "NEED_PASSWORD":  # the owner password opens it as well: ask for that one
            raise ValueError("SOURCE_PROTECTED") from exc
        raise
    every = permissions_int(ALL_PERMS)
    if info.get("protected") and not info.get("owner") and (info.get("perms", -1) & every) != every:
        doc.close()
        raise ValueError("SOURCE_PROTECTED")
    return doc


def check_source(path: str, password: str | None = None) -> None:
    """Raise what :func:`load_source` would, before anything is asked or written. Only an encrypted file
    is read in full."""
    try:
        with pymupdf.open(path) as src:  # reads the cross-reference table, not the pages
            if not src.is_pdf or _encrypt_dict(src) is None:
                return
    except Exception:  # load_source says what is wrong with it
        pass
    load_source(path, password).close()


def insert_pdf_file(doc: pymupdf.Document, path: str, at: int, password: str | None = None) -> int:
    """Insert every page of another file before index ``at`` (an image becomes one page). Returns the page
    count added."""
    src = load_source(path, password)
    try:
        at = max(0, min(at, len(doc)))
        doc.insert_pdf(src, start_at=at)
        return len(src)
    finally:
        src.close()


def insert_images(doc: pymupdf.Document, paths: Sequence[str], at: int) -> int:
    """Insert one page per image before index ``at``, page sized to the image. Through load_source, so a
    protected PDF under another name is refused like any other."""
    at = max(0, min(at, len(doc)))
    added = 0
    for p in paths:
        added += insert_pdf_file(doc, p, at + added)
    if not added:
        raise ValueError("no images inserted")
    return added


def duplicate_pages(doc: pymupdf.Document, pages: Sequence[int]) -> list[int]:
    """Copy each page right after itself. Returns the indices of the copies."""
    made = []
    for offset, pno in enumerate(sorted(set(pages))):
        src = pno + offset
        doc.fullcopy_page(src, src + 1 if src + 1 < len(doc) else -1)  # PyMuPDF takes -1 for the end, not len(doc)
        made.append(src + 1)
    return made


def crop_pages(doc: pymupdf.Document, pages: Sequence[int], frac: Sequence[float]) -> None:
    """Crop to the box ``frac`` = (x0, y0, x1, y1) given as fractions of the page on screen.

    Fractions let one drawn box apply to pages of different sizes or rotations.
    """
    fx0, fy0, fx1, fy1 = (min(1.0, max(0.0, float(v))) for v in frac)
    if fx1 - fx0 < 0.02 or fy1 - fy0 < 0.02:
        raise ValueError("crop box too small")
    for pno in pages:
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        # straight to PDF user space: PyMuPDF's set_cropbox() measures y from the MediaBox top, its mediabox from 0
        r = pymupdf.Rect(fx0 * w, fy0 * h, fx1 * w, fy1 * h) * ~page_ctm(page) & page.mediabox
        _set_box(doc, page, "CropBox", r)


def uncrop_pages(doc: pymupdf.Document, pages: Sequence[int]) -> None:
    for pno in pages:
        page = doc[pno]
        _set_box(doc, page, "CropBox", page.mediabox)


def _set_box(doc, page, key: str, r: pymupdf.Rect) -> None:
    set_page_key(page, key, pymupdf.mupdf.pdf_new_rect(pymupdf._as_pdf_document(doc), pymupdf.mupdf.FzRect(*r)))


# --- multi-file operations ----------------------------------------------------


def merge(paths: Sequence[str], out_path: str, passwords: dict[str, str] | None = None) -> int:
    """Combine whole files into a new, unprotected PDF (see :func:`load_source` for protected ones)."""
    from . import annots  # imports this module

    passwords = passwords or {}
    out = pymupdf.open()
    try:
        for p in paths:
            src = load_source(p, passwords.get(p))
            out.insert_pdf(src)
            src.close()
        annots.forget_passwords(out)  # as in every file this program writes
        write_atomic(out_path, lambda path: out.save(path, garbage=3, deflate=True))
        return len(out)
    finally:
        out.close()


def copy_pages(doc: pymupdf.Document, pages: Sequence[int]) -> pymupdf.Document:
    """A new document of ``pages``, in that order. One graft map for all of them (``final`` only on the
    last), so fonts and images the pages share are copied once and not once per page."""
    out = pymupdf.open()
    for k, pno in enumerate(pages):
        out.insert_pdf(doc, from_page=pno, to_page=pno, final=k == len(pages) - 1)
    return out


def extract_pages(doc: pymupdf.Document, pages: Sequence[int], out_path: str, protection: dict | None = None) -> None:
    """Write a new PDF containing only ``pages`` (in the given order). The pages keep the protection
    of their document: pass it as Session.save_protection gives it, like a Save As copy."""
    out = copy_pages(doc, pages)
    try:
        save_pdf(out, out_path, **protection_args(protection))
    finally:
        out.close()


def split_doc(doc: pymupdf.Document, out_dir: str, n: int, base: str, protection: dict | None = None) -> list[str]:
    """Split into chunks of ``n`` pages, protected like :func:`extract_pages`. Returns the written file paths."""
    n = max(1, int(n))
    written: list[str] = []
    for start in range(0, len(doc), n):
        end = min(start + n - 1, len(doc) - 1)
        out = pymupdf.open()
        out.insert_pdf(doc, from_page=start, to_page=end)
        path = free_path(os.path.join(out_dir, f"{base}_{start + 1}-{end + 1}.pdf"))
        save_pdf(out, path, **protection_args(protection))
        out.close()
        written.append(path)
    return written


def images_to_pdf(image_paths: Sequence[str], out_path: str) -> int:
    out = pymupdf.open()
    try:
        insert_images(out, image_paths, 0)
        write_atomic(out_path, lambda path: out.save(path, garbage=3, deflate=True))
        return len(out)
    finally:
        out.close()


# --- rendering / text / search / metadata ------------------------------------

MAX_RENDER_PIXELS = 36_000_000


def render_pixmap(page: pymupdf.Page, width_px: int) -> pymupdf.Pixmap:
    """Render ``page`` at ``width_px`` device pixels wide (aspect kept), RGB without alpha."""
    w, h = page.rect.width, page.rect.height
    scale = min(width_px / w, 10.0)  # no floor: a thumbnail of a giant page is a thumbnail too
    if w * h * scale * scale > MAX_RENDER_PIXELS:
        scale = (MAX_RENDER_PIXELS / (w * h)) ** 0.5
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)


def encode_jpeg(pix: pymupdf.Pixmap, quality: int = 88) -> bytes:
    """JPEG bytes of an RGB pixmap. Pillow's libjpeg-turbo is several times faster than
    MuPDF's own encoder (about 20 ms against 150 ms for a full-width page) and it does
    not need the document, so callers can encode outside the document lock."""
    from PIL import Image

    img = Image.frombuffer("RGB", (pix.width, pix.height), pix.samples_mv, "raw", "RGB", pix.stride, 1)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def render_jpeg(page: pymupdf.Page, width_px: int, quality: int = 88) -> bytes:
    """Render ``page`` at ``width_px`` device pixels wide (aspect kept) as JPEG."""
    return encode_jpeg(render_pixmap(page, width_px), quality)


def search_rects(doc: pymupdf.Document, query: str, limit: int = 5000) -> list[dict]:
    """All matches as {"p": page, "r": [x0,y0,x1,y1]} in display space, reading order."""
    out: list[dict] = []
    query = (query or "").strip()
    if not query:
        return out
    for i, page in enumerate(doc):
        m = turn(page)
        for r in page.search_for(query):
            out.append({"p": i, "r": rounded(r * m)})
            if len(out) >= limit:
                return out
    return out


def get_metadata(doc: pymupdf.Document) -> dict:
    return dict(doc.metadata or {})


def set_metadata(doc: pymupdf.Document, meta: dict) -> None:
    current = get_metadata(doc)
    current.update({k: v for k, v in meta.items() if k in ("title", "author", "subject", "keywords", "creator", "producer")})
    doc.set_metadata(current)


def fonts_used(doc: pymupdf.Document, limit: int = 200) -> list[dict]:
    seen: dict[str, dict] = {}
    for pno in range(len(doc)):
        for xref, ext, ftype, basefont, *_ in doc.get_page_fonts(pno):
            name = (basefont or "?").split("+")[-1]
            if name not in seen:
                seen[name] = {"name": name, "type": ftype, "embedded": ext not in ("", "n/a")}
        if len(seen) >= limit:
            break
    return sorted(seen.values(), key=lambda f: f["name"].lower())


# --- bookmarks ----------------------------------------------------------------


def get_toc(doc: pymupdf.Document) -> list[list]:
    """[[level, title, page_index, k], ...] - page index 0-based (-1 if the target is missing);
    k is the entry's place in the outline, so set_toc can keep its exact target."""
    return [[lvl, title, page - 1, k] for k, (lvl, title, page, *_) in enumerate(doc.get_toc(simple=False))]


def set_toc(doc: pymupdf.Document, entries: Sequence[Sequence]) -> None:
    """Replace the outline. Levels are clamped so the hierarchy is always valid. Entries that
    come from get_toc (with k) keep their target (position on the page, web link); new ones
    point at the top of their page."""
    old = doc.get_toc(simple=False)
    toc = []
    prev = 0
    for lvl, title, page, *rest in entries:
        lvl = max(1, min(int(lvl), prev + 1))
        item = [lvl, str(title)[:500] or "-", int(page) + 1]
        k = rest[0] if rest else -1
        if isinstance(k, int) and 0 <= k < len(old):
            item = [lvl, item[1], old[k][2], old[k][3]]
        toc.append(item)
        prev = lvl
    doc.set_toc(toc)


# --- attachments --------------------------------------------------------------


def list_attachments(doc: pymupdf.Document) -> list[dict]:
    out = []
    for name in doc.embfile_names():
        info = doc.embfile_info(name)
        out.append({"name": name, "file": info.get("filename") or name, "size": info.get("size", 0),
                    "desc": info.get("description", "")})
    return out


def add_attachment(doc: pymupdf.Document, path: str) -> str:
    name = os.path.basename(path)
    existing = set(doc.embfile_names())
    base, ext = os.path.splitext(name)
    k = 1
    while name in existing:
        k += 1
        name = f"{base} ({k}){ext}"
    with open(path, "rb") as f:
        doc.embfile_add(name, f.read(), filename=os.path.basename(path))
    return name


def save_attachment(doc: pymupdf.Document, name: str, out_path: str) -> None:
    write_atomic(out_path, doc.embfile_get(name))


def delete_attachment(doc: pymupdf.Document, name: str) -> None:
    doc.embfile_del(name)


# --- exports (new files; session untouched) ----------------------------------

Progress = Callable[[int, int], bool] | None  # (done, total) -> keep going?


class Cancelled(BaseException):
    """Raised from progress callbacks. BaseException so libraries that swallow
    ``Exception`` per page (pdf2docx does) still stop."""


def _tick(progress: Progress, done: int, total: int) -> None:
    if progress is not None and progress(done, total) is False:
        raise Cancelled()


# The exports below take ``lock`` around each page's MuPDF work and write their files outside it: given the
# session lock, the viewer goes on drawing pages while a long export runs (MuPDF allows one thread at a time).
_NO_LOCK = contextlib.nullcontext()


def export_text(doc: pymupdf.Document, out_path: str, pages: Sequence[int] | None = None, lock=_NO_LOCK) -> None:
    parts = []
    for pno in range(len(doc)) if pages is None else pages:
        with lock:
            parts.append(doc[pno].get_text())
    write_atomic(out_path, "\n\f\n".join(parts).encode("utf-8"))


def capped_dpi(page: pymupdf.Page, dpi: int) -> int:
    """``dpi``, lowered as far as a page of that size needs to stay within MAX_RENDER_PIXELS."""
    area = max(page.rect.width * page.rect.height, 1.0) / (72 * 72)  # square inches
    return max(1, min(int(dpi), int((MAX_RENDER_PIXELS / area) ** 0.5)))


def export_page_images(doc, pages, out_dir, base, fmt="png", dpi=150, progress: Progress = None,
                       lock=_NO_LOCK) -> list[str]:
    fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
    written = []
    for k, pno in enumerate(pages):
        with lock:
            page = doc[pno]
            pix = page.get_pixmap(dpi=capped_dpi(page, dpi), alpha=False)
            data = None if fmt == "jpg" else pix.tobytes("png")
        path = free_path(os.path.join(out_dir, f"{base}_{pno + 1}.{fmt}"))
        with open(path, "wb") as f:
            f.write(encode_jpeg(pix, 92) if data is None else data)
        written.append(path)
        _tick(progress, k + 1, len(pages))
    return written


MAX_IMAGE_PIXELS = 256_000_000  # an image saved as PNG is decoded first: a small file can hold gigabytes of pixels


def extract_embedded_images(doc, pages, out_dir, base, progress: Progress = None, lock=_NO_LOCK) -> list[str]:
    """Save every distinct embedded image on ``pages`` in its native format (none bigger than MAX_IMAGE_PIXELS)."""
    seen: set[int] = set()
    written = []
    for k, pno in enumerate(pages):
        with lock:
            images = doc[pno].get_images(full=True)  # (xref, smask, width, height, ...)
        for xref, _smask, width, height, *_ in images:
            if xref in seen:
                continue
            seen.add(xref)
            if width * height > MAX_IMAGE_PIXELS:
                log.warning("embedded image %d (%dx%d) skipped: too big", xref, width, height)
                continue
            with lock:
                data = doc.extract_image(xref)
            if not data or not data.get("image"):
                continue
            path = free_path(os.path.join(out_dir, f"{base}_p{pno + 1}_{xref}.{data.get('ext', 'png')}"))
            with open(path, "wb") as f:
                f.write(data["image"])
            written.append(path)
        _tick(progress, k + 1, len(pages))
    return written


def compress_to(doc: pymupdf.Document | bytes, out_path: str, *, image_dpi: int = 150, quality: int = 75,
                protection: dict | None = None, progress: Progress = None, lock=_NO_LOCK) -> tuple[int, int]:
    """Write a smaller copy: downsample big images, subset fonts, deflate everything.

    Returns the sizes before and after. The session document is not modified. The image pass is one
    MuPDF call, which big scans keep busy for minutes: it runs in a process of its own, so a cancel
    (``progress`` returning False) stops it too, like the steps before and after it. ``doc`` may be the
    document's bytes already; ``lock`` is held only for the steps in this process (see export_text).
    """
    if isinstance(doc, bytes):
        data = smaller = doc
    else:
        with lock:
            data = smaller = doc.tobytes()
    _tick(progress, 0, 2)
    if image_dpi:
        try:  # in memory both ways: a protected document is not written to disk unprotected
            smaller = _in_process(_images_process, (data, int(image_dpi), int(quality)),
                                  None if progress is None else lambda _done, _total: progress(0, 2), "the image pass")
        except RuntimeError:  # shortcut: keep going with lossless savings only
            log.warning("compress: the image pass failed", exc_info=True)
    _tick(progress, 1, 2)
    with lock:
        tmp = pymupdf.open("pdf", smaller)
        try:
            save_pdf(tmp, out_path, compress=True, subset_fonts=True, **protection_args(protection))
        finally:
            tmp.close()
    return len(data), os.path.getsize(out_path)


def _images_process(data: bytes, dpi: int, quality: int, conn) -> None:
    """The child process of :func:`compress_to`: big images made smaller, the PDF sent back."""
    pymupdf.set_messages(pylogging=True)  # MuPDF warnings into logging: pythonw has no console
    try:
        doc = pymupdf.open("pdf", data)
        doc.rewrite_images(dpi_threshold=int(dpi * 1.3), dpi_target=dpi, quality=quality)
        conn.send(("done", doc.tobytes()))
    except Exception as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        raise SystemExit(1)  # the exit code says it too, in case the message is lost with the pipe
    finally:
        conn.close()


def export_docx(pdf_path: str, out_path: str, progress: Progress = None, password: str | None = None) -> None:
    """Convert a PDF file to Word with pdf2docx, in a process of its own.

    The conversion can take minutes and uses PyMuPDF all along, which must not run on two threads of
    one process: in its own process it leaves the open document free, and a crash in it cannot take
    the app down. ``progress`` returning False stops that process.
    """
    _in_process(_docx_process, (pdf_path, out_path, password), progress, "the Word export")


def _in_process(target, args: tuple, progress: Progress, what: str):
    """Run ``target(*args, conn)`` in a process of its own, which sends ("progress", done, total), ("done",
    result) or ("error", text) through ``conn``. Returns the result; ``progress`` returning False stops the
    process (Cancelled), and an error or a crash in it raises RuntimeError."""
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=target, args=(*args, writer), daemon=True)
    proc.start()
    writer.close()  # only the child writes now: the end of the pipe is the end of the child
    done = total = 0
    error = result = None
    piped = True
    try:
        while piped or proc.is_alive():  # until the child is gone: a cancel still stops it if the pipe fails first
            msg = None
            if not piped:
                proc.join(0.25)
            else:
                try:
                    msg = reader.recv() if reader.poll(0.25) else None
                except (EOFError, OSError):  # the child has closed its end (Windows says so in poll)
                    piped = False
            if msg and msg[0] == "progress":
                done, total = msg[1], msg[2]
            elif msg and msg[0] == "done":
                result = msg[1]
            elif msg:
                error = msg[1]
            _tick(progress, done, total)  # also between messages, so a cancel does not wait for the next page
    except BaseException:
        proc.terminate()
        raise
    finally:
        proc.join()
        reader.close()
    if error or proc.exitcode:
        raise RuntimeError(error or f"{what} stopped (exit code {proc.exitcode})")
    return result


def _docx_process(pdf_path: str, out_path: str, password: str | None, conn) -> None:
    """The child process of :func:`export_docx`: converts, reports pdf2docx's "(3/10)" progress lines."""
    class Report(logging.Handler):
        pattern = re.compile(r"\((\d+)/(\d+)\)")

        def emit(self, record):
            m = self.pattern.search(record.getMessage())
            if m:
                conn.send(("progress", int(m.group(1)), int(m.group(2))))

    root = logging.getLogger()
    root.addHandler(Report())  # before pdf2docx is imported, so its basicConfig leaves logging alone
    root.setLevel(logging.INFO)
    pymupdf.set_messages(pylogging=True)  # MuPDF warnings into logging too: pythonw has no console
    try:
        from pdf2docx import Converter

        cv = Converter(pdf_path, password=password)
        try:
            cv.convert(out_path, multi_processing=False)
        finally:
            cv.close()
    except Exception as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        raise SystemExit(1)  # the exit code says it too, in case the message is lost with the pipe
    finally:
        conn.close()
