"""Engine tests: every core operation on a real PyMuPDF document.

Rotated pages are checked in display space (what the user sees), because that is where
the v1 engine went wrong. Run: python -m pytest tests
"""
import math
import os
import re
import time

import pymupdf

import pytest

from conftest import line_angles, make_doc, png_bytes, save_tmp, words_display
from core import annots as an
from core import pdftools as pt

ROTATIONS = (0, 90, 180, 270)


# --- basics ------------------------------------------------------------------------


def test_parse_page_range():
    assert pt.parse_page_range("1-3,5,8-", 10) == [0, 1, 2, 4, 7, 8, 9]
    assert pt.parse_page_range("all", 3) == [0, 1, 2]
    assert pt.parse_page_range("sve", 2) == [0, 1]
    assert pt.parse_page_range("2", 3) == [1]
    with pytest.raises(ValueError):
        pt.parse_page_range("99", 3)
    t0 = time.perf_counter()
    assert pt.parse_page_range("0-1, 2-30000000", 3) == [0, 1, 2]  # a typo must not count to 30 million
    assert time.perf_counter() - t0 < 0.5


def test_permissions_roundtrip():
    assert pt.permissions_int([]) == 0
    v = pt.permissions_int(["print", "copy"])
    assert v & pymupdf.PDF_PERM_PRINT and v & pymupdf.PDF_PERM_COPY and not v & pymupdf.PDF_PERM_MODIFY


# --- open / protect / save -------------------------------------------------------------


def _protected(tmp_path, user, owner, allowed):
    out = os.path.join(tmp_path, f"p_{user}_{owner}.pdf")
    pt.save_pdf(make_doc(2), out, user_pw=user, owner_pw=owner, allowed=allowed)
    return out


def test_load_plain_copy_and_protection_info(tmp_path):
    path = _protected(tmp_path, "u1", "o1", ["print"])
    with pytest.raises(ValueError, match="NEED_PASSWORD"):
        pt.load_for_editing(path)
    with pytest.raises(ValueError, match="WRONG_PASSWORD"):
        pt.load_for_editing(path, "nope")

    doc, info = pt.load_for_editing(path, "u1")
    assert not doc.needs_pass and "Aurora sample page 1" in doc[0].get_text()
    assert info["protected"] and not info["owner"] and info["algorithm"] == "aes256"
    assert info["user_pw"] == "u1" and info["owner_pw"] is None
    rights = pt.rights_for(info)
    assert rights["print"] and not rights["modify"] and not rights["copy"]

    doc, info = pt.load_for_editing(path, "o1")
    assert info["owner"] and info["owner_pw"] == "o1" and info["user_pw"] is None
    assert all(pt.rights_for(info).values())


def test_owner_only_protection(tmp_path):
    path = _protected(tmp_path, "", "boss", ["print", "annotate"])
    doc, info = pt.load_for_editing(path)  # opens without a password
    assert info["protected"] and not info["owner"] and info["user_pw"] == ""
    r = pt.rights_for(info)
    assert r["annotate"] and r["print"] and not r["modify"]
    _, info2 = pt.load_for_editing(path, "boss")
    assert info2["owner"]


def test_save_keeps_permissions_and_xrefs(tmp_path):
    doc = make_doc(2)
    a = doc[0].add_rect_annot((50, 300, 150, 360))
    xref_before = a.xref
    out = os.path.join(tmp_path, "enc.pdf")
    perms = pt.permissions_int(["print", "copy"])
    pt.save_pdf(doc, out, user_pw="x", owner_pw="y", perms=perms)
    # the live document is untouched (garbage collection happened on a copy)
    assert doc[0].load_annot(xref_before) is not None
    d2, info = pt.load_for_editing(out, "x")
    assert info["perms"] & pymupdf.PDF_PERM_COPY and not info["perms"] & pymupdf.PDF_PERM_MODIFY
    assert len(list(d2[0].annots())) == 1


def test_open_image_converts_to_pdf(tmp_path):
    p = os.path.join(tmp_path, "scan.png")
    with open(p, "wb") as f:
        f.write(png_bytes(300, 400))
    doc, info = pt.load_for_editing(p)
    assert info["converted"] and len(doc) == 1


# --- watermark / header-footer upright on rotated pages ----------------------------------


def drawn_points(page):
    """Every path point the page draws, in display space (a text watermark is drawn, not written)."""
    pts = []
    for d in page.get_drawings():
        for item in d["items"]:
            pts += [pymupdf.Point(v) * page.rotation_matrix for v in item[1:] if isinstance(v, pymupdf.Point)]
    return pts


def axis_angle(pts):
    """Visual angle (degrees, counter-clockwise on screen) of the long axis of a cloud of points."""
    n = len(pts)
    mx, my = sum(p.x for p in pts) / n, sum(p.y for p in pts) / n
    sxx = sum((p.x - mx) ** 2 for p in pts)
    syy = sum((p.y - my) ** 2 for p in pts)
    sxy = sum((p.x - mx) * (p.y - my) for p in pts)
    return round(-math.degrees(0.5 * math.atan2(2 * sxy, sxx - syy))) % 180


@pytest.mark.parametrize("rot", ROTATIONS)
def test_watermark_text_upright_and_centered(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    pt.watermark_text(doc, "POVERLJIVO Đ", rotate=0, fontsize=40, opacity=0.3)
    box = pymupdf.Rect(drawn_points(page)[0], drawn_points(page)[0])
    for q in drawn_points(page):
        box |= q
    assert box.width > box.height * 4, "watermark should read horizontally on screen"
    assert abs((box.x0 + box.x1) / 2 - page.rect.width / 2) < 10
    assert abs((box.y0 + box.y1) / 2 - page.rect.height / 2) < 30


@pytest.mark.parametrize("rot", ROTATIONS)
def test_watermark_angle_is_visual(rot):
    doc = make_doc(1, rotation=rot)
    pt.watermark_text(doc, "DRAFT COPY", rotate=45, fontsize=40)
    assert abs(axis_angle(drawn_points(doc[0])) - 45) <= 3


def test_watermark_cannot_be_selected_or_copied(tmp_path):
    """A watermark on a document locked against changes and copying must not come out as text in any reader."""
    doc = make_doc(1)
    pt.watermark_text(doc, "Ђорђе POVERLJIVO čćđ", tile=True, fontsize=24)
    path = tmp_path / "locked.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="vlasnik", user_pw="",
             permissions=pymupdf.PDF_PERM_PRINT)
    again = pymupdf.open(path)
    text = again[0].get_text()
    assert "POVERLJIVO" not in text and "Ђорђе" not in text and "čćđ" not in text
    assert "Aurora sample page 1" in text  # the page's own text is untouched
    assert len(drawn_points(again[0])) > 200  # tiled: drawn several times


def test_cjk_watermark_draws_real_glyphs_centered():
    """Noto Sans has no Chinese or Japanese; the watermark then takes the built-in CJK font, whose glyphs have
    curves (the missing-glyph box is four straight lines) and whose widths keep the text centered."""
    doc = make_doc(1)
    page = doc[0]
    pt.watermark_text(doc, "机密 社外秘", rotate=0, fontsize=40)
    kinds = {item[0] for d in page.get_drawings() for item in d["items"]}
    assert "c" in kinds
    pts = drawn_points(page)
    box = pymupdf.Rect(pts[0], pts[0])
    for q in pts:
        box |= q
    assert abs((box.x0 + box.x1) / 2 - page.rect.width / 2) < 10
    assert "机密" not in page.get_text()


def test_cjk_header_footer_is_written_in_a_font_that_has_it():
    doc = make_doc(2)
    pt.add_header_footer(doc, text="第 {n} 页，共 {total} 页", position="bc")
    assert "第 2 页，共 2 页" in doc[1].get_text()
    assert any(f[3].startswith("Droid Sans Fallback") for f in doc[1].get_fonts())
    pt.add_header_footer(doc, text="Side {n}", position="tc")  # Latin text keeps Noto Sans
    assert any("Noto" in f[3] for f in doc[0].get_fonts())


@pytest.mark.parametrize("rot", ROTATIONS)
def test_header_footer_positions(rot):
    doc = make_doc(3, rotation=rot)
    pt.add_header_footer(doc, text="Strana {n} od {total}", position="br", fontsize=10)
    page = doc[1]
    words = [(r, t) for r, t in words_display(page) if t in ("Strana", "od", "3")]
    assert len(words) >= 3
    box = words[0][0]
    for r, _ in words:
        box |= r
    assert box.width > box.height
    assert box.x1 > page.rect.width * 0.7 and box.y1 > page.rect.height * 0.85
    assert "Strana 2 od 3" in page.get_text().replace("\n", " ")


def test_header_footer_date_follows_the_ui():
    doc = make_doc(1)
    pt.add_header_footer(doc, text="Dato {date}", date_text="13.10.2026")
    assert "Dato 13.10.2026" in doc[0].get_text()


def test_header_footer_keeps_other_braces():
    doc = make_doc(2)
    pt.add_header_footer(doc, text="{Fortrolig} side {n} av {total}")
    assert "{Fortrolig} side 2 av 2" in doc[1].get_text()


@pytest.mark.parametrize("rot", (0, 90))
def test_watermark_image_upright(rot):
    doc = make_doc(1, rotation=rot)
    pt.watermark_image(doc, png_bytes(200, 100), opacity=1.0, scale=0.5)
    page = doc[0]
    pix = page.get_pixmap(dpi=36)
    # find the blue marker: it must be at the top-left of the red stamp on screen
    blue = red = None
    for y in range(pix.height):
        for x in range(pix.width):
            r, g, b = pix.pixel(x, y)[:3]
            if b > 180 and r < 80 and blue is None:
                blue = (x, y)
            if r > 180 and g < 80 and b < 80:
                red = red or (x, y)
    assert blue and red
    assert blue[0] <= red[0] + 2 and blue[1] <= red[1] + 2


# --- page operations -------------------------------------------------------------------


def test_rotate_delete_reorder_insert_duplicate(tmp_path):
    doc = make_doc(3)
    pt.rotate_pages(doc, [0], 90)
    assert doc[0].rotation == 90
    pt.reorder_pages(doc, [2, 1, 0])
    assert "page 3" in doc[0].get_text()
    pt.delete_pages(doc, [1])
    assert len(doc) == 2
    with pytest.raises(ValueError):
        pt.delete_pages(doc, [0, 1])
    at = pt.insert_blank(doc, 1, 300, 300)
    assert at == 1 and len(doc) == 3 and doc[1].rect.width == 300
    made = pt.duplicate_pages(doc, [0])
    assert made == [1] and len(doc) == 4 and doc[1].get_text() == doc[0].get_text()
    other = save_tmp(make_doc(2), tmp_path, "other.pdf")
    assert pt.insert_pdf_file(doc, other, 0) == 2 and len(doc) == 6
    img = os.path.join(tmp_path, "i.png")
    with open(img, "wb") as f:
        f.write(png_bytes())
    assert pt.insert_images(doc, [img, img], len(doc)) == 2 and len(doc) == 8


def test_duplicate_several_pages_and_the_last():
    doc = make_doc(3)
    assert pt.duplicate_pages(doc, [2, 0]) == [1, 4]
    assert [p.get_text().split()[3] for p in doc] == ["1", "1", "2", "3", "3"]


@pytest.mark.parametrize("rot", (0, 90))
def test_crop_twice(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    pt.crop_pages(doc, [0], (0.1, 0.1, 0.9, 0.6))
    page = doc[0]
    assert abs(page.rect.width - w * 0.8) < 1 and abs(page.rect.height - h * 0.5) < 1
    pt.crop_pages(doc, [0], (0.0, 0.0, 0.5, 0.5))
    page = doc[0]
    assert abs(page.rect.width - w * 0.4) < 1 and abs(page.rect.height - h * 0.25) < 1
    pt.uncrop_pages(doc, [0])
    assert abs(doc[0].rect.width - w) < 1


@pytest.mark.parametrize("rot", (0, 90, 180, 270))
@pytest.mark.parametrize("box", ("cropped", "crop past the media box", "user unit 2"))
def test_points_land_in_place_on_a_cropped_turned_page(rot, box):
    """A signature box and a moved drawing go where the user put them: PyMuPDF's page matrix leaves out the
    CropBox offset of a turned page, which put a signature off the page. A CropBox reaching past the MediaBox
    (MuPDF clips it) and /UserUnit (MuPDF scales the page) moved them too."""
    from core import signing

    doc = make_doc(1)
    if box == "cropped":
        pt.crop_pages(doc, [0], (0.1, 0.2, 0.9, 0.9))
    else:
        doc.xref_set_key(doc[0].xref, *(("CropBox", "[100 100 900 900]") if box.startswith("crop") else ("UserUnit", "2")))
    page = doc.reload_page(doc[0])
    page.set_rotation(rot)
    x0, y0, x1, y1 = signing.box_from_display(page, (40, 40, 120, 80))
    blue = doc.get_new_xref()  # drawn in PDF user space, where the signature goes
    doc.update_object(blue, "<<>>")
    doc.update_stream(blue, f"q 0 0 1 rg {x0} {y0} {x1 - x0} {y1 - y0} re f Q".encode())
    doc.xref_set_key(page.xref, "Contents", f"[{page.get_contents()[0]} 0 R {blue} 0 R]")
    pix = page.get_pixmap(dpi=72)
    assert pix.pixel(80, 60)[:3] == (0, 0, 255) and pix.pixel(150, 60)[:3] != (0, 0, 255)
    xref = an.add_ink(page, [[(60, 100), (100, 130)]])
    before = {a["x"]: a["r"] for a in an.page_annots(page)}[xref]
    an.move_annot(page, xref, 10, 0)
    after = {a["x"]: a["r"] for a in an.page_annots(page)}[xref]
    assert abs(after[0] - before[0] - 10) < 1 and abs(after[1] - before[1]) < 1


ODD_BOXES = {
    "cropped": {"CropBox": "[60 80 552 742]"},
    "media box away from 0,0": {"MediaBox": "[100 200 712 992]", "CropBox": "[150 260 650 950]"},
    "crop past the media box": {"CropBox": "[100 100 900 900]"},
    "user unit 2": {"UserUnit": "2"},
}


def _odd_page(box: str, rot: int, mark: bool = True):
    """A page with an unusual box, turned by ``rot``; with ``mark`` a black MARK written by hand in PDF space."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for k, v in ODD_BOXES[box].items():
        doc.xref_set_key(page.xref, k, v)
    if mark:
        doc.xref_set_key(page.xref, "Resources", "<</Font<</F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")
        c = doc.get_new_xref()
        doc.update_object(c, "<<>>")
        doc.update_stream(c, b"BT /F9 30 Tf 200 400 Td (MARK) Tj ET")
        doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    page = doc.reload_page(page)
    page.set_rotation(rot)
    return doc, page


def _ink(page, color: str = "black", clip=None):
    """Where the pixels of one colour (black or green) are on screen, in display space, within ``clip``."""
    from PIL import Image, ImageChops

    pix = page.get_pixmap(dpi=72, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    x, y = (int(clip[0]), int(clip[1])) if clip else (0, 0)
    if clip:
        img = img.crop((x, y, int(clip[2]), int(clip[3])))
    r, g, b = img.split()
    green = color == "green"
    mask = ImageChops.darker(ImageChops.darker(r.point(lambda v: 255 if v < 80 else 0),
                                               g.point(lambda v: 255 if (v > 200 if green else v < 80) else 0)),
                             b.point(lambda v: 255 if v < 80 else 0))
    box = mask.getbbox()
    return pymupdf.Rect(box) + (x, y, x, y) if box else None


def page_ctm(page) -> pymupdf.Matrix:
    """PDF user space -> what the page shows, straight from MuPDF (the test's own yardstick)."""
    m = pymupdf.mupdf
    ctm = m.FzMatrix()
    m.pdf_page_transform(pymupdf._as_pdf_page(page), m.FzRect(), ctm)
    return pymupdf.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)


def _near(a, b, tol=3):
    return a is not None and b is not None and max(abs(u - v) for u, v in zip(a, b)) < tol


def _within(ink, box, slack=3):
    """The drawn glyphs sit inside the reported word box (font boxes are taller than the ink)."""
    return ink is not None and (pymupdf.Rect(box) + (-slack, -slack, slack, slack)).contains(ink)


@pytest.mark.parametrize("rot", (0, 90, 180, 270))
@pytest.mark.parametrize("box", tuple(ODD_BOXES))
def test_what_is_read_shows_where_it_is_on_any_page(box, rot):
    """Words, search hits and links come back where the page shows them. PyMuPDF's page.rotation_matrix turns
    about the CropBox as written, which is off when MuPDF clips it or scales by /UserUnit, and links come in
    display space already: on every turned page a link's hotspot sat elsewhere, and redaction missed it."""
    doc, page = _odd_page(box, rot)
    ink = _ink(page)
    word = next(w for w in an.text_layer(page) if w[5] == "MARK")
    left, top, length, thick, ang = word[:5]
    shown = {0: (left, top, left + length, top + thick), 90: (left - thick, top, left, top + length),
             180: (left - length, top - thick, left, top), 270: (left, top - length, left + thick, top)}[ang]
    assert ang == rot and _within(ink, shown)
    assert _within(ink, pt.search_rects(doc, "MARK")[0]["r"])
    links = []
    for y, uri in ((300, "https://example.com/?pnr=12345678901"), (500, "https://example.com/stays")):
        links.append(doc.get_new_xref())
        doc.update_object(links[-1], f"<</Type/Annot/Subtype/Link/Rect[220 {y} 300 {y + 30}]/A<</S/URI/URI({uri})>>>>")
    doc.xref_set_key(page.xref, "Annots", f"[{links[0]} 0 R {links[1]} 0 R]")
    page = doc.reload_page(page)
    where = pymupdf.Rect(220, 300, 300, 330) * page_ctm(page)
    assert _near(an.page_links(page)[0]["r"], where)
    an.mark_redaction(page, tuple(where + (-2, -2, 2, 2)))
    an.apply_redactions(doc)
    assert [lk["uri"] for lk in doc[0].get_links()] == ["https://example.com/stays"]


def test_redaction_takes_links_after_a_crop_and_a_redaction():
    """A crop wrote the page's dictionary anew (xref_set_key puts a copy in its place), and so did the fill of a
    redaction; MuPDF finds the links it takes by the objects of the /Annots it loaded: the link stayed in its list
    of the page, and reading the page's links failed."""
    doc = pymupdf.open()
    page = doc.new_page()
    xs = []
    for y, uri in ((200, "https://example.com/a"), (400, "https://example.com/b"), (600, "https://example.com/c")):
        xs.append(doc.get_new_xref())
        doc.update_object(xs[-1], f"<</Type/Annot/Subtype/Link/Rect[220 {y} 300 {y + 30}]/A<</S/URI/URI({uri})>>>>")
    doc.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in xs) + "]")
    doc = pymupdf.open("pdf", doc.tobytes())
    page = doc[0]  # held throughout: MuPDF keeps a page it loaded, links and all, while it is held
    pt.crop_pages(doc, [0], (0, 0, 1, 0.98))
    for gone, left in (("/a", ["https://example.com/b", "https://example.com/c"]), ("/b", ["https://example.com/c"])):
        r = next(lk["r"] for lk in an.page_links(page) if lk["u"].endswith(gone))
        an.mark_redaction(page, tuple(pymupdf.Rect(r) + (-2, -2, 2, 2)))
        assert an.apply_redactions(doc) == 1
        assert [lk["u"] for lk in an.page_links(page)] == left


@pytest.mark.parametrize("rot", (0, 90, 180, 270))
@pytest.mark.parametrize("box", tuple(ODD_BOXES))
def test_what_is_added_lands_where_it_was_put_on_any_page(box, rot):
    """Drawings, stamps, watermarks, page numbers and crops land where the user put them. PyMuPDF takes each in
    another space: a picture through ~page.transformation_matrix (no CropBox position on a turned page), text
    from the CropBox as written, and without /UserUnit; a crop measured y from the MediaBox top."""
    doc, page = _odd_page(box, rot, mark=False)
    w, h = page.rect.width, page.rect.height
    want = pymupdf.Rect(0.3 * w, 0.3 * h, 0.3 * w + 80, 0.3 * h + 50)
    an.add_shape(page, "rect", tuple(want), width=0, fill="#00ff00", color="#00ff00")
    assert _near(_ink(page, "green"), want)

    doc, page = _odd_page(box, rot, mark=False)
    pt.watermark_image(doc, png_bytes(100, 50, (0, 255, 0, 255), mark=False), opacity=1, scale=0.5)
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    assert _near(_ink(page, "green"), (w / 4, h / 2 - w / 8, 3 * w / 4, h / 2 + w / 8))

    doc, page = _odd_page(box, rot, mark=False)
    pt.watermark_text(doc, "MARK", opacity=1, rotate=0, fontsize=30, color="#000000")
    pt.add_header_footer(doc, text="MARK", position="bc", fontsize=20, color="#000000", margin=40)
    page = doc[0]
    w, h = page.rect.width, page.rect.height
    mid, foot = _ink(page, clip=(0, h * 0.25, w, h * 0.75)), _ink(page, clip=(0, h * 0.85, w, h))
    assert _near(((mid.x0 + mid.x1) / 2, (mid.y0 + mid.y1) / 2), (w / 2, h / 2))  # the watermark, in the middle
    assert _near(((foot.x0 + foot.x1) / 2, foot.y1), (w / 2, h - 40))  # the page number, on its line

    doc, page = _odd_page(box, rot)
    before = _ink(page)
    w, h = page.rect.width, page.rect.height
    pt.crop_pages(doc, [0], (0.1, 0.1, 0.9, 0.9))
    page = doc[0]
    assert _near((page.rect.width, page.rect.height), (0.8 * w, 0.8 * h))
    assert _near(_ink(page), before + (-0.1 * w, -0.1 * h, -0.1 * w, -0.1 * h))
    pt.uncrop_pages(doc, [0])  # the whole MediaBox
    page = doc[0]
    full = pymupdf.Rect(page.mediabox) * page_ctm(page)
    assert _near((page.rect.width, page.rect.height), (full.width, full.height))


def test_merge_extract_split_images(tmp_path):
    src = save_tmp(make_doc(3), tmp_path, "src.pdf")
    out = os.path.join(tmp_path, "m.pdf")
    assert pt.merge([src, src], out) == 6
    doc = make_doc(3)
    pt.extract_pages(doc, [2, 0], os.path.join(tmp_path, "e.pdf"))
    e = pymupdf.open(os.path.join(tmp_path, "e.pdf"))
    assert len(e) == 2 and "page 3" in e[0].get_text()
    # pages that share a font and a logo: the extract holds them once, not once per page
    letter = pymupdf.open()
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300), False)
    logo.set_rect(logo.irect, (200, 30, 30))
    xref = 0
    for i in range(12):
        page = letter.new_page()
        page.insert_text((50, 100), f"Side {i + 1} Čačak", fontname="noto", fontfile=str(pt.FONT_SANS), fontsize=14)
        xref = page.insert_image(pymupdf.Rect(400, 20, 560, 180), pixmap=logo, xref=xref)
    letter = pymupdf.open("pdf", letter.tobytes(garbage=3, deflate=True))
    pt.extract_pages(letter, range(12), os.path.join(tmp_path, "letter.pdf"))
    assert os.path.getsize(os.path.join(tmp_path, "letter.pdf")) < 1.2 * len(letter.tobytes(garbage=3, deflate=True))
    files = pt.split_doc(doc, str(tmp_path), 2, "part")
    assert len(files) == 2 and all(os.path.exists(f) for f in files)
    img = os.path.join(tmp_path, "i.png")
    with open(img, "wb") as f:
        f.write(png_bytes())
    assert pt.images_to_pdf([img, img], os.path.join(tmp_path, "imgs.pdf")) == 2


# --- render / text / search ----------------------------------------------------------------



def test_bookmark_edit_keeps_the_other_targets():
    doc = make_doc(3)
    doc.set_toc([
        [1, "Intro", 1, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(72, 500), "page": 0}],
        [1, "Results", 2, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(72, 400), "page": 1}],
        [1, "Website", -1, {"kind": pymupdf.LINK_URI, "uri": "https://example.com/"}],
    ])
    items = pt.get_toc(doc)
    items[0][1] = "Introduction"  # rename one
    items.append([1, "New", 2])   # and add one
    pt.set_toc(doc, items)
    toc = doc.get_toc(simple=False)
    assert [t[1] for t in toc] == ["Introduction", "Results", "Website", "New"]
    assert round(toc[0][3]["to"].y) == 500 and round(toc[1][3]["to"].y) == 400
    assert toc[2][3]["kind"] == pymupdf.LINK_URI and toc[2][3]["uri"] == "https://example.com/"
    assert toc[3][2] == 3

def test_render_jpeg_and_search_rects():
    doc = make_doc(2, rotation=90)
    jpg = pt.render_jpeg(doc[0], 400)
    assert jpg[:2] == b"\xff\xd8"
    hits = pt.search_rects(doc, "marker-2")
    assert len(hits) == 1 and hits[0]["p"] == 1
    x0, y0, x1, y1 = hits[0]["r"]
    assert x1 - x0 > y1 - y0  # horizontal on screen even though the page is rotated
    assert [h["p"] for h in pt.search_rects(doc, "lorem")] == [0, 1]


def test_metadata_toc_attachments_fonts(tmp_path):
    doc = make_doc(3)
    pt.set_metadata(doc, {"title": "Naslov", "author": "Marko", "bogus": "x"})
    assert pt.get_metadata(doc)["title"] == "Naslov"
    pt.set_toc(doc, [[1, "Uvod", 0], [3, "Detalj", 1], [1, "Kraj", 2]])  # bad level is clamped
    assert pt.get_toc(doc) == [[1, "Uvod", 0, 0], [2, "Detalj", 1, 1], [1, "Kraj", 2, 2]]
    f = os.path.join(tmp_path, "note.txt")
    with open(f, "w") as fh:
        fh.write("hello")
    name = pt.add_attachment(doc, f)
    assert pt.add_attachment(doc, f) != name
    assert [a["name"] for a in pt.list_attachments(doc)][:1] == [name]
    out = os.path.join(tmp_path, "out.txt")
    pt.save_attachment(doc, name, out)
    assert open(out).read() == "hello"
    pt.delete_attachment(doc, name)
    assert len(pt.list_attachments(doc)) == 1
    assert any(f["embedded"] for f in pt.fonts_used(doc))


def test_exports(tmp_path):
    doc = make_doc(2)
    doc[0].insert_image((50, 300, 250, 400), stream=png_bytes())
    pt.export_text(doc, os.path.join(tmp_path, "t.txt"))
    assert "Čačak" in open(os.path.join(tmp_path, "t.txt"), encoding="utf-8").read()
    imgs = pt.export_page_images(doc, [0, 1], str(tmp_path), "pg", "jpg", 72)
    assert len(imgs) == 2 and all(os.path.getsize(p) > 0 for p in imgs)
    emb = pt.extract_embedded_images(doc, [0, 1], str(tmp_path), "emb")
    assert len(emb) == 1
    calls = []
    with pytest.raises(pt.Cancelled):
        pt.export_page_images(doc, [0, 1], str(tmp_path), "c", "png", 72, progress=lambda d, t: (calls.append(d), False)[1])
    assert calls == [1]  # stopped after the first page


def test_compress_shrinks_big_images(tmp_path):
    from PIL import Image
    import io
    import random

    doc = make_doc(1)
    img = Image.new("RGB", (2400, 2400))
    px = img.load()
    rnd = random.Random(1)
    for y in range(0, 2400, 4):
        for x in range(0, 2400, 4):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc[0].insert_image((0, 0, 200, 200), stream=buf.getvalue())
    plain = os.path.join(tmp_path, "plain.pdf")
    pt.save_pdf(doc, plain)
    small = os.path.join(tmp_path, "small.pdf")
    before, size = pt.compress_to(doc, small, image_dpi=100, quality=60)
    assert size < os.path.getsize(plain) * 0.7 < before
    stopped = os.path.join(tmp_path, "stopped.pdf")
    with pytest.raises(pt.Cancelled):  # Cancel: no file
        pt.compress_to(doc, stopped, progress=lambda done, total: False)
    assert not os.path.exists(stopped)


def test_compress_stops_in_the_image_pass(tmp_path):
    """The image pass is one MuPDF call, which big scans keep busy for minutes, and a cancel waited for it. It
    runs in a process of its own now, which a cancel stops."""
    import multiprocessing

    calls, out = [], os.path.join(tmp_path, "stopped.pdf")
    with pytest.raises(pt.Cancelled):  # yes before the pass, no at the first look while it runs
        pt.compress_to(make_doc(1), out, progress=lambda done, total: (calls.append(done), len(calls) < 2)[1])
    assert calls == [0, 0] and not os.path.exists(out) and not multiprocessing.active_children()


@pytest.mark.parametrize("case", ("0 0 Td", "'", "0 Tc in a drawing"))
def test_compress_leaves_text_in_place(tmp_path, case):
    """Compressing had MuPDF write the content anew (clean=True), which moved text as its redaction did: an
    overprint followed on from the first, ' lines left the page."""
    doc, _ = _steady_doc(case)
    before, out = _away(doc[0]), str(tmp_path / "small.pdf")
    pt.compress_to(doc, out, image_dpi=0)
    assert _same(before, _away(pymupdf.open(out)[0]))


def test_docx_export(tmp_path):
    pytest.importorskip("pdf2docx")
    src = save_tmp(make_doc(2), tmp_path, "w.pdf")
    out = os.path.join(tmp_path, "w.docx")
    seen = []
    pt.export_docx(src, out, progress=lambda d, t: seen.append((d, t)) or True)
    assert os.path.getsize(out) > 1000 and seen
    # the conversion runs in a process of its own: its error comes back as the reason, not as a broken pipe
    with pytest.raises(RuntimeError, match="no such file"):
        pt.export_docx(os.path.join(tmp_path, "gone.pdf"), out)


def test_docx_export_stops_on_cancel_after_its_pipe_failed(tmp_path, monkeypatch):
    """It used to wait for the whole conversion once the pipe failed, past any cancel."""
    pytest.importorskip("pdf2docx")
    import multiprocessing.connection as mpc

    def broken(self, timeout=0.0):
        raise OSError("the pipe broke")

    monkeypatch.setattr(mpc._ConnectionBase, "poll", broken)  # the parent's end only: the child is a new process
    src = save_tmp(make_doc(2), tmp_path, "w.pdf")
    with pytest.raises(pt.Cancelled):
        pt.export_docx(src, os.path.join(tmp_path, "w.docx"), progress=lambda d, t: False)


# --- page overlay data ------------------------------------------------------------------------


@pytest.mark.parametrize("rot", ROTATIONS)
def test_text_layer_geometry(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    layer = an.text_layer(page)
    texts = [w[5] for w in layer]
    assert "Aurora" in texts and "Čačak" in texts
    first = layer[0]
    left, top, length, thick, angle, text, end = first
    assert angle == 0  # upright on screen whatever the page rotation
    # the word starts near x=50 on screen and its box sits above the baseline at y=80
    assert 45 < left < 55 and 55 < top < 80 and length > thick
    assert layer[3][6] == 1 or any(w[6] for w in layer)


def test_page_info_annots_links_fields():
    doc = make_doc(1)
    page = doc[0]
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 60, 200, 85), "uri": "https://example.com"})
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.rect = "ime", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(50, 400, 250, 425)
    page.add_widget(w)
    cb = pymupdf.Widget()
    cb.field_name, cb.field_type, cb.rect = "slazem", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.Rect(50, 440, 65, 455)
    page.add_widget(cb)
    an.add_note(page, (300, 300), "Pogledaj ovo", author="Marko")
    page = doc.reload_page(page)  # links are cached per Page object; the session always loads fresh pages
    info = an.page_info(page)
    assert info["links"] and info["links"][0]["u"] == "https://example.com"
    kinds = {f["n"]: f for f in info["fields"]}
    assert kinds["ime"]["k"] == "text" and kinds["slazem"]["k"] == "check"
    assert info["annots"][0]["t"] == "Text" and info["annots"][0]["by"] == "Marko"
    an.set_field(page, kinds["ime"]["x"], "Marko Vesel")
    an.set_field(page, kinds["slazem"]["x"], True)
    fields = {f["n"]: f for f in an.page_fields(page)}
    assert fields["ime"]["v"] == "Marko Vesel" and fields["slazem"]["v"] is True
    assert an.reset_fields(doc) == 2
    assert {f["n"]: f["v"] for f in an.page_fields(page)} == {"ime": "", "slazem": False}
    an.set_field(page, kinds["ime"]["x"], "Trajno")
    an.flatten(doc)
    assert not doc.is_form_pdf and "Trajno" in doc[0].get_text()


def _field(page, kind, name, rect, **props):
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = kind, name, pymupdf.Rect(rect)
    for k, v in props.items():
        setattr(w, k, v)
    page.add_widget(w)
    return next(w for w in page.widgets() if w.field_name == name).xref


def test_a_password_stays_out_of_the_saved_file(tmp_path):
    """ISO 32000-1, 12.7.4.3: readers should never store the value of a password field in the file. The
    open document keeps it, so the field still holds it on screen."""
    doc = make_doc(1)
    page = doc[0]
    pin = _field(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "pin", (50, 400, 250, 425), field_flags=an.FF_PASSWORD)
    name = _field(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "ime", (50, 300, 250, 325))
    an.set_field(page, pin, "tajna")
    an.set_field(page, name, "Marko")
    out = tmp_path / "out.pdf"
    pt.save_pdf(doc, str(out))
    assert b"tajna" not in out.read_bytes() and b"tajna" not in pt.clean_bytes(doc)  # also what gets signed
    saved = pymupdf.open(out)
    assert {f["n"]: f["v"] for f in an.page_fields(saved[0])} == {"pin": "", "ime": "Marko"}
    assert "•" not in saved[0].get_text() and {f["n"]: f["v"] for f in an.page_fields(page)}["pin"] == "tajna"
    field, kids = doc.get_new_xref(), (doc.get_new_xref(), doc.get_new_xref())
    doc.update_object(field, f"<</FT/Tx/T(pin2)/Ff 8192/DA(/Helv 0 Tf 0 g)/V(tajna1)/Kids[{kids[0]} 0 R {kids[1]} 0 R]>>")
    for k, y in zip(kids, (500, 540)):  # the second with a value of its own, as some programs write them
        doc.update_object(k, f"<</Type/Annot/Subtype/Widget/Rect[50 {y} 250 {y + 25}]/Parent {field} 0 R/F 4"
                             + ("/V(tajna2)>>" if k == kids[1] else ">>"))
    doc.xref_set_key(page.xref, "Annots", doc.xref_get_key(page.xref, "Annots")[1][:-1] + f" {kids[0]} 0 R {kids[1]} 0 R]")
    doc = pymupdf.open("pdf", doc.tobytes())  # the page's widget list is read when it is loaded
    page = doc[0]
    assert b"tajna" not in pt.clean_bytes(doc)
    doc.save(tmp_path / "raw.pdf")  # as a program that stores it would
    pt.merge([str(tmp_path / "raw.pdf")], str(tmp_path / "merged.pdf"))
    assert b"tajna" not in (tmp_path / "merged.pdf").read_bytes()
    an.set_field(page, kids[0], "nova")  # the field gets the value, and no widget keeps an old one of its own
    assert doc.xref_get_key(field, "V") == ("string", "nova") and doc.xref_get_key(kids[1], "V")[0] == "null"


def test_emptying_a_field_does_not_bring_back_its_default():
    doc = make_doc(1)
    page = doc[0]
    xref = _field(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "ime", (50, 400, 250, 425), field_value="Podrazumevano")
    doc.xref_set_key(xref, "DV", "(Podrazumevano)")
    an.set_field(page, xref, "Marko")
    an.set_field(page, xref, "")  # the user deleted the text
    assert an.page_fields(page)[0]["v"] == ""
    assert "Podrazumevano" not in page.get_text("text", clip=pymupdf.Rect(50, 400, 250, 425))
    assert pymupdf.open("pdf", doc.tobytes())[0].first_widget.field_value == ""
    an.reset_fields(doc)  # "reset the fields" still means back to the defaults
    assert an.page_fields(page)[0]["v"] == "Podrazumevano"


def test_dropdown_options_keep_value_and_label_apart():
    """A choice can store "NO" and show "Norway"; the file must get the stored value."""
    doc = make_doc(1)
    page = doc[0]
    xref = _field(page, pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "land", (50, 400, 250, 425),
                  choice_values=[["RS", "Serbia"], ["NO", "Norway"]], field_value="NO")
    doc.xref_set_key(xref, "DV", "(NO)")
    plain = _field(page, pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "avdeling", (50, 440, 250, 465),
                   choice_values=["Analyse", "Økonomi"], field_value="Analyse")
    fields = {f["n"]: f for f in an.page_fields(page)}
    assert fields["land"]["opts"] == [["RS", "Serbia"], ["NO", "Norway"]] and fields["land"]["v"] == "NO"
    assert fields["avdeling"]["opts"] == [["Analyse", "Analyse"], ["Økonomi", "Økonomi"]]
    an.set_field(page, xref, "RS")
    an.set_field(page, plain, "Økonomi")
    assert doc.xref_get_key(xref, "V") == ("string", "RS")
    saved = pymupdf.open("pdf", doc.tobytes())
    assert {f["n"]: f["v"] for f in an.page_fields(saved[0])} == {"land": "RS", "avdeling": "Økonomi"}

    def shown():  # what the boxes themselves show (print, export, flatten, other viewers)
        baked = pymupdf.open("pdf", doc.tobytes())
        baked.bake()
        return baked[0].get_text().split()

    assert "Serbia" in shown() and "RS" not in shown() and "Økonomi" in shown()
    an.set_field(page, xref, "")  # the empty choice empties the box as well
    assert doc.xref_get_key(xref, "V") == ("string", "") and "Serbia" not in shown()
    an.reset_fields(doc)  # back to the default, shown by its label
    assert doc.xref_get_key(xref, "V") == ("string", "NO") and "Norway" in shown() and "NO" not in shown()


def _shown(doc, rect, pno=0):
    """The words a field shows once the fields are burnt into the page, as print, export and other readers
    see them."""
    baked = pymupdf.open("pdf", doc.tobytes())
    baked.bake()
    r = pymupdf.Rect(rect)
    return [(w[4], pymupdf.Rect(w[:4])) for w in baked[pno].get_text("words")
            if r.contains((pymupdf.Rect(w[:4]).tl + pymupdf.Rect(w[:4]).br) / 2)]


def _fits(shown, rect, text):
    """Every word of ``text`` in the box, readable (MuPDF drew Č, Ć, Đ at size 0) and inside its width."""
    r = pymupdf.Rect(rect)
    return [w for w, _ in shown] == text.split() and all(b.height > 5 and r.x0 <= b.x0 and b.x1 <= r.x1
                                                         for _, b in shown)


def test_serbian_letters_show_in_the_fields():
    """Text outside Windows-1252 takes MuPDF's other font, which it drew at size 0 in a field that sizes
    its text itself and below the box in a one-line field."""
    doc = make_doc(1)
    page = doc[0]
    TEXT = pymupdf.PDF_WIDGET_TYPE_TEXT
    cases = [  # (box, field, value, what the box shows)
        ((50, 300, 300, 325), dict(), "Marko Veselinović", None),
        ((50, 340, 300, 380), dict(text_fontsize=11), "Đurđevac i Čačak", None),
        ((50, 400, 300, 470), dict(field_flags=an.FF_MULTILINE), "Љубљана, Ђаковица и Ниш у пољу које прелама ред", None),
        ((50, 490, 300, 515), dict(field_type=pymupdf.PDF_WIDGET_TYPE_COMBOBOX, field_value="DR",
                                   choice_values=[["MO", "Морава"], ["DR", "Drina"]]), "MO", "Морава"),
    ]
    xrefs = []
    for i, (box, props, value, _) in enumerate(cases):
        xrefs.append(_field(page, props.pop("field_type", TEXT), f"f{i}", box, **props))
        an.set_field(page, xrefs[-1], value)
    for (box, _, value, label), xref in zip(cases, xrefs):
        assert _fits(_shown(doc, box), box, label or value), value
    assert doc.xref_get_key(xrefs[0], "DA")[1].endswith(" 0 Tf")  # the field still sizes its text itself:
    long_name = "Marko Veselinović Đorđević Ćirković Šćekić"  # a longer name gets smaller
    an.set_field(page, xrefs[0], long_name)
    assert _fits(_shown(doc, cases[0][0]), cases[0][0], long_name)
    saved = pymupdf.open("pdf", doc.tobytes())
    assert [f["v"] for f in an.page_fields(saved[0])] == [long_name] + [c[2] for c in cases[1:]]


@pytest.mark.parametrize("rot", (0, 90))
def test_sizes_come_as_on_screen_on_a_page_with_a_user_unit(rot):
    """On a page with /UserUnit 2 a unit of the file is two points on screen. A free text, a frame and a line
    showed twice as big as asked, the panel read the frame's width back in units of the file, and a field that
    sizes its text itself got twice the size MuPDF gives it (Č, Ć and Đ take a font that we size)."""
    seen = []
    for unit in (1, 2):
        doc = pymupdf.open()
        doc.new_page(width=612 / unit, height=792 / unit)  # the same size on screen
        doc.xref_set_key(doc.page_xref(0), "UserUnit", str(unit))
        page = doc[0]
        page.set_rotation(rot)
        an.add_freetext(page, (60, 60, 400, 120), "HHHH", fontsize=20)
        sq = an.add_shape(page, "rect", (60, 200, 200, 300), width=4)
        an.add_shape(page, "line", (250, 250, 400, 250), width=6)
        field = _field(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "ime", (60, 400, 300, 430))
        sizes = []
        for value in ("Durdevac", "Đurđevac"):  # MuPDF sizes the first, we size the second
            an.set_field(page, field, value)
            ap = doc.xref_stream(int(doc.xref_get_key(field, "AP/N")[1].split()[0]))
            sizes.append(float(re.search(rb" ([\d.]+) Tf", ap).group(1)))
        assert sizes[0] == sizes[1], sizes
        page = doc[0]
        pix = page.get_pixmap(dpi=72, alpha=False)

        def dark(xs, ys) -> int:
            return sum(1 for x in xs for y in ys if sum(pix.pixel(x, y)) < 450)

        info = next(a for a in an.page_annots(page) if a["x"] == sq)
        seen.append((dark([100], range(60, 120)), dark([130], range(190, 210)), dark([320], range(240, 260)),
                     info["w"]))
    assert seen[0] == seen[1] and seen[0][3] == 4, seen


def _form_made_elsewhere():
    """A form the way other programs make them: /AcroForm an object of its own that asks readers to draw the
    fields, values changed after the drawings were made, the form's own font, rich text and no border, a
    value longer than MaxLen, actions shared by two fields, a password, a hidden helper field without a box,
    and one field on two pages whose second widget names the wrong page."""
    doc = make_doc(2)
    page = doc[0]
    x = {name: _field(page, pymupdf.PDF_WIDGET_TYPE_TEXT, name, box, field_value="Stari", **props)
         for name, box, props in (("ime", (50, 300, 300, 325), {}), ("grad", (50, 340, 300, 365), {}),
                                  ("pin", (50, 380, 300, 405), {"field_flags": an.FF_PASSWORD}),
                                  ("skriveno", (0, 0, 10, 10), {}))}
    for name, value in (("ime", "Marko Veselinović"), ("grad", "Čačak"), ("pin", "tajna"), ("skriveno", "x")):
        doc.xref_set_key(x[name], "V", pymupdf.get_pdf_str(value))  # the drawings still say "Stari"
    for key, value in (("DA", "(/ArialMT 0 Tf 0 0 1 rg)"), ("DS", "(font: Arial)"), ("BS", "<</S/S/W 0>>"),
                       ("MaxLen", "3")):
        doc.xref_set_key(x["grad"], key, value)
    doc.xref_set_key(x["skriveno"], "Rect", "[0 0 0 0]")
    doc.xref_set_key(x["ime"], "RV", "(<body><p>Marko Veselinović</p></body>)")
    aa = doc.get_new_xref()
    doc.update_object(aa, "<</K<</S/ResetForm>>>>")  # not JavaScript, which PyMuPDF drops
    for name in ("ime", "grad"):
        doc.xref_set_key(x[name], "AA", f"{aa} 0 R")
    reka, kids = doc.get_new_xref(), (doc.get_new_xref(), doc.get_new_xref())  # one field, two widgets
    doc.update_object(reka, f"<</FT/Tx/T(reka)/DA(/Helv 0 Tf 0 g)/V{pymupdf.get_pdf_str('Морава')}"
                            f"/DV{pymupdf.get_pdf_str('Морава')}/Kids[{kids[0]} 0 R {kids[1]} 0 R]>>")
    for kid, pno in zip(kids, (0, 1)):
        px = doc.page_xref(pno)
        doc.update_object(kid, f"<</Type/Annot/Subtype/Widget/Rect[50 30 300 55]/Parent {reka} 0 R/F 4"
                               f"/P {doc.page_xref(0)} 0 R>>")
        kind, annots = doc.xref_get_key(px, "Annots")
        doc.xref_set_key(px, "Annots", (annots[:-1] if kind == "array" else "[") + f" {kid} 0 R]")
    cat = doc.pdf_catalog()
    kind, form = doc.xref_get_key(cat, "AcroForm")
    af = int(form.split()[0]) if kind == "xref" else doc.get_new_xref()
    if kind != "xref":
        doc.update_object(af, form)
        doc.xref_set_key(cat, "AcroForm", f"{af} 0 R")
    doc.xref_set_key(af, "Fields", doc.xref_get_key(af, "Fields")[1][:-1] + f" {reka} 0 R]")
    doc.xref_set_key(af, "NeedAppearances", "true")
    return pymupdf.open("pdf", doc.tobytes()), af, x | {"aa": aa, "reka": kids[0]}


def test_a_form_that_asks_readers_to_draw_its_fields():
    """NeedAppearances: MuPDF drew such fields for the screen only, without Č, Ć, Đ, and flattening burnt
    in the old drawings. Opening draws them once, and nothing else of the fields changes."""
    doc, af, x = _form_made_elsewhere()
    own = {k: doc.xref_get_key(x["grad"], k) for k in ("DA", "DS", "BS", "MaxLen")}
    actions = doc.xref_object(x["aa"], compressed=True)
    an.settle_fields(doc)
    assert doc.xref_get_key(af, "NeedAppearances") == ("bool", "false")
    for box, text in (((50, 300, 300, 325), "Marko Veselinović"), ((50, 340, 300, 365), "Čačak")):
        assert _fits(_shown(doc, box), box, text), text
    reka = (50, 540, 300, 565)
    assert _fits(_shown(doc, reka), reka, "Морава") and _fits(_shown(doc, reka, 1), reka, "Морава")
    assert {k: doc.xref_get_key(x["grad"], k) for k in own} == own  # its font, rich text, border and MaxLen
    assert "tajna" not in _shown(doc, (50, 380, 300, 405))[0][0]  # a password is never drawn readable
    values = {"ime": "Marko Veselinović", "grad": "Čačak", "pin": "tajna", "skriveno": "x", "reka": "Морава"}
    assert {f["n"]: f["v"] for f in an.page_fields(doc[0])} == values
    assert "Veselinović" in doc[0].get_text()  # the screen shows the same
    assert doc.xref_get_key(x["ime"], "RV")[0] == "string"  # same value, same rich text
    an.set_field(doc[0], x["grad"], "Novi Sad")  # typing keeps to MaxLen, and keeps the field as it is
    assert doc.xref_get_key(x["grad"], "V") == ("string", "Nov")
    assert {k: doc.xref_get_key(x["grad"], k) for k in own} == own
    an.set_field(doc[0], x["ime"], "Novo ime")
    assert doc.xref_get_key(x["ime"], "RV")[0] == "null"  # else readers would show the old rich text
    assert doc.xref_object(x["aa"], compressed=True) == actions
    an.set_field(doc[0], x["reka"], "Ниш")
    assert an.reset_fields(doc) == 6  # the hidden field and both widgets of "reka" too, as one field
    for pno in (0, 1):
        doc[pno].get_pixmap(dpi=20)  # rendering a page must not draw a widget again its own way
        assert _fits(_shown(doc, reka, pno), reka, "Морава"), pno


def test_a_copied_page_shows_what_its_fields_hold():
    """A copied page's widget names its field as parent, but the field's /Kids does not list it."""
    doc, af, x = _form_made_elsewhere()
    doc.xref_set_key(af, "NeedAppearances", "false")  # so the widgets of "reka" are not drawn yet
    pt.duplicate_pages(doc, [0])
    an.set_field(doc[0], x["reka"], "Đerdap")
    reka = (50, 540, 300, 565)
    for pno in (0, 1, 2):  # the page, its copy, and the field's widget on the old second page
        doc[pno].get_pixmap(dpi=20)  # MuPDF draws a widget that has no drawing yet, its own way
        assert _fits(_shown(doc, reka, pno), reka, "Đerdap"), pno


def _drawing(doc, on):
    """A button drawing of the form's own: a blue square for on, nothing for off."""
    x = doc.get_new_xref()
    doc.update_object(x, "<</Type/XObject/Subtype/Form/BBox[0 0 14 14]>>")
    doc.update_stream(x, b"0 0 1 rg 2 2 10 10 re f" if on else b"")
    return x


def _buttons():
    """A check box shown on two pages and a radio group of three, every widget with the form's own drawings."""
    doc = make_doc(2)
    fields = {}
    for name, flags, kids in (("da", 0, (("Yes", 0, 700), ("Yes", 1, 700))),
                              ("izbor", 49152, (("A", 0, 650), ("B", 0, 600), ("C", 1, 650)))):
        field, xs = doc.get_new_xref(), []
        for state, pno, y in kids:
            x = doc.get_new_xref()
            doc.update_object(x, f"<</Type/Annot/Subtype/Widget/Rect[50 {y} 64 {y + 14}]/Parent {field} 0 R/F 4"
                                 f"/AP<</N<</{state} {_drawing(doc, True)} 0 R/Off {_drawing(doc, False)} 0 R>>>>/AS/Off>>")
            kind, annots = doc.xref_get_key(doc.page_xref(pno), "Annots")
            doc.xref_set_key(doc.page_xref(pno), "Annots", (annots[:-1] if kind == "array" else "[") + f" {x} 0 R]")
            xs.append(x)
        doc.update_object(field, f"<</FT/Btn/Ff {flags}/T({name})/V/Off/Kids[{' '.join(f'{x} 0 R' for x in xs)}]>>")
        fields[name] = (field, xs)
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", "<</Fields[" + " ".join(f"{f} 0 R" for f, _ in fields.values()) + "]>>")
    return pymupdf.open("pdf", doc.tobytes()), fields


def test_check_boxes_and_radio_buttons_change_in_every_place():
    """A check box or radio group shown in several places changes everywhere at once. PyMuPDF changed only the
    clicked widget, left a check box on when it was turned off through its other widget, wrote /V as text
    and swapped the form's own drawings for its own."""
    doc, b = _buttons()
    drawings = {x: doc.xref_get_key(x, "AP/N") for _, xs in b.values() for x in xs}

    def shown(name):
        return [doc.xref_get_key(x, "AS")[1] for x in b[name][1]]

    an.set_field(doc[1], b["da"][1][1], True)  # through the widget on page 2
    assert doc.xref_get_key(b["da"][0], "V") == ("name", "/Yes") and shown("da") == ["/Yes", "/Yes"]
    an.set_field(doc[0], b["da"][1][0], False)
    assert doc.xref_get_key(b["da"][0], "V") == ("name", "/Off") and shown("da") == ["/Off", "/Off"]
    an.set_field(doc[1], b["izbor"][1][2], True)
    assert doc.xref_get_key(b["izbor"][0], "V") == ("name", "/C") and shown("izbor") == ["/Off", "/Off", "/C"]
    an.set_field(doc[0], b["izbor"][1][1], True)
    assert shown("izbor") == ["/Off", "/B", "/Off"]
    assert [f["v"] for p in doc for f in an.page_fields(p)] == [False, False, True, False, False]
    assert {x: doc.xref_get_key(x, "AP/N") for x in drawings} == drawings  # the form's own on and off
    assert all(doc.xref_get_key(x, "V")[0] == "null" for x in drawings)  # the value stays on the field
    pt.duplicate_pages(doc, [0])  # a copied page's widgets name their field as parent, /Kids does not list them
    copy = [x for x, *_ in doc[1].annot_xrefs()]
    an.set_field(doc[0], b["da"][1][0], True)
    assert [doc.xref_get_key(x, "AS")[1] for x in copy] == ["/Yes", "/Off", "/B"]
    doc.xref_set_key(b["izbor"][0], "DV", "/C")
    assert an.reset_fields(doc) == 8
    assert shown("da") == ["/Off", "/Off"] and shown("izbor") == ["/Off", "/Off", "/C"]
    assert [doc.xref_get_key(x, "AS")[1] for x in copy] == ["/Off", "/Off", "/Off"]
    for p in doc:
        p.get_pixmap(dpi=20)  # MuPDF's own reset had the buttons drawn its own way at the next render
    assert {x: doc.xref_get_key(x, "AP/N") for x in drawings} == drawings


def test_the_field_layer_shows_each_button_as_its_widget_shows_it():
    """On or off comes from each widget's own /AS. A check box whose widgets have different states showed both
    on, a state name with # in it never showed on, and a button without drawings stopped the page's whole
    field layer. A radio button without drawings turns on alone."""
    doc = make_doc(1)
    pol, bez, xs = doc.get_new_xref(), doc.get_new_xref(), []
    for i, state in enumerate(("M", "Ja#2FNei")):  # the second is "Ja/Nei"
        x = doc.get_new_xref()
        doc.update_object(x, f"<</Type/Annot/Subtype/Widget/Rect[{50 + 40 * i} 700 {64 + 40 * i} 714]/Parent {pol} 0 R"
                             f"/F 4/AP<</N<</{state} {_drawing(doc, True)} 0 R/Off {_drawing(doc, False)} 0 R>>>>/AS/Off>>")
        xs.append(x)
    for i in range(2):
        x = doc.get_new_xref()
        doc.update_object(x, f"<</Type/Annot/Subtype/Widget/Rect[{50 + 40 * i} 600 {64 + 40 * i} 614]/Parent {bez} 0 R/F 4>>")
        xs.append(x)
    doc.update_object(pol, f"<</FT/Btn/T(pol)/V/Off/Kids[{xs[0]} 0 R {xs[1]} 0 R]>>")
    doc.update_object(bez, f"<</FT/Btn/Ff 49152/T(bez)/Kids[{xs[2]} 0 R {xs[3]} 0 R]>>")
    doc.xref_set_key(doc.page_xref(0), "Annots", "[" + " ".join(f"{x} 0 R" for x in xs) + "]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{pol} 0 R {bez} 0 R]>>")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.set_field(doc[0], xs[1], True)
    an.set_field(doc[0], xs[3], True)
    assert [doc.xref_get_key(x, "AS")[1] for x in xs] == ["/Off", "/Ja/Nei", "/Off", "/Yes"]
    assert [f["v"] for f in an.page_fields(doc[0])] == [False, True, False, True]


def test_a_form_that_cannot_be_drawn_keeps_asking_readers(monkeypatch):
    doc, af, _ = _form_made_elsewhere()
    monkeypatch.setattr(an, "_widgets", lambda page: iter(()) if page.number == 0 else 1 / 0)  # page 2 unreadable
    with pytest.raises(ZeroDivisionError):
        an.settle_fields(doc)
    assert doc.xref_get_key(af, "NeedAppearances") == ("bool", "true")


def _list_marks(doc, boxes):
    """For each list box, which of its options print with the selection colour behind them."""
    baked = pymupdf.open("pdf", doc.tobytes())
    baked.bake()
    page = baked[0]
    marks = [item[1] for d in page.get_drawings() if d.get("fill") == pytest.approx((0.6, 0.757, 0.855), abs=0.01)
             for item in d["items"] if item[0] == "re"]
    words = [(w[4], (pymupdf.Rect(w[:4]).tl + pymupdf.Rect(w[:4]).br) / 2) for w in page.get_text("words")]
    out = {}
    for name, (box, labels) in boxes.items():
        found = {t: any(r.contains(m) for r in marks) for t, m in words if pymupdf.Rect(box).contains(m)}
        out[name] = [found.get(label.split()[0], "not printed") for label in labels]
    return out


def test_list_boxes_mark_what_is_chosen():
    """MuPDF draws a list box without marking the chosen options, so print, export and other readers showed
    them all alike. The chosen ones get the selection colour behind them, in both of MuPDF's layouts (one of
    them for options with Č, Ć, Đ)."""
    doc = make_doc(1)
    page = doc[0]
    LIST = pymupdf.PDF_WIDGET_TYPE_LISTBOX
    cities, langs = ["Beograd", "Novi Sad", "Čačak", "Ниш"], ["Srpski", "Norsk", "English", "Deutsch"]
    boxes = {"grad": ((50, 300, 250, 380), cities), "jezici": ((300, 300, 500, 380), langs)}
    one = _field(page, LIST, "grad", boxes["grad"][0], field_value="NS",
                 choice_values=[[c[:2].upper(), c] for c in cities])
    more = _field(page, LIST, "jezici", boxes["jezici"][0], field_value="Sr", field_flags=an.FF_MULTISELECT,
                  choice_values=[[c[:2], c] for c in langs])
    an.set_field(page, one, "ČA")
    an.set_field(page, more, ["Sr", "De"])
    assert _list_marks(doc, boxes) == {"grad": [False, False, True, False], "jezici": [True, False, False, True]}
    an.set_field(page, one, "BE")  # the options decide the font, not the choice: MuPDF drew them at size 0
    assert _list_marks(doc, boxes)["grad"] == [True, False, False, False]
    an.reset_fields(doc)  # no default: nothing chosen
    assert _list_marks(doc, boxes) == {"grad": [False] * 4, "jezici": [False] * 4}
    doc.xref_set_key(one, "TI", "1")  # scrolled: MuPDF draws the list from its second option
    an.set_field(page, one, "ČA")
    assert _list_marks(doc, boxes)["grad"] == ["not printed", False, True, False]


def test_a_form_that_asks_readers_to_draw_its_lists():
    """Opening a form with NeedAppearances switches it off, so a list box gets its marks then, not at the first
    click. A value stored as a name stays."""
    doc = make_doc(1)
    cities, box = ["Beograd", "Novi Sad", "Subotica"], (50, 300, 250, 380)
    xref = _field(doc[0], pymupdf.PDF_WIDGET_TYPE_LISTBOX, "grad", box, choice_values=cities, field_value="Beograd")
    doc.xref_set_key(xref, "V", "(Subotica)")  # chosen in a program that left the drawing to the readers
    land = _field(doc[0], pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "land", (50, 400, 250, 425),
                  choice_values=[["RS", "Serbia"], ["NO", "Norway"]], field_value="RS")
    doc.xref_set_key(land, "V", "/NO")
    doc.xref_set_key(*an._need_appearances(doc), "true")
    doc = pymupdf.open("pdf", doc.tobytes())
    assert _list_marks(doc, {"grad": (box, cities)})["grad"] == [False, False, False]
    an.settle_fields(doc)
    assert _list_marks(doc, {"grad": (box, cities)})["grad"] == [False, False, True]
    assert [f["v"] for f in an.page_fields(doc[0])] == ["Subotica", "NO"]
    assert "Norway" in doc[0].get_text("text", clip=pymupdf.Rect(50, 400, 250, 425))


def test_a_broken_field_does_not_stop_filling_or_saving():
    """A check box whose /Parent is no dictionary is a field of its own. A field whose /Parent loops MuPDF cannot
    read, and saving skips it."""
    doc = make_doc(2)
    box, num = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(num, "42")
    doc.update_object(box, f"<</Type/Annot/Subtype/Widget/FT/Btn/Rect[50 500 64 514]/F 4/Parent {num} 0 R>>")
    doc.xref_set_key(doc.page_xref(0), "Annots", f"[{box} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{box} 0 R]>>")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.set_field(doc[0], box, True)
    assert doc.xref_get_key(box, "AS") == ("name", "/Yes") and an.reset_fields(doc) == 1
    assert doc.xref_get_key(box, "AS") == ("name", "/Off")
    text, a, b = (doc.get_new_xref() for _ in range(3))
    doc.update_object(text, f"<</Type/Annot/Subtype/Widget/FT/Tx/V(x)/Rect[50 500 250 525]/F 4/Parent {a} 0 R>>")
    doc.update_object(a, f"<</Parent {b} 0 R>>")
    doc.update_object(b, f"<</Parent {a} 0 R>>")
    doc.xref_set_key(doc.page_xref(1), "Annots", f"[{text} 0 R]")
    assert b"/Widget" in pt.clean_bytes(doc)


def test_list_with_several_choices():
    doc = make_doc(1)
    page = doc[0]
    xref = _field(page, pymupdf.PDF_WIDGET_TYPE_LISTBOX, "jezici", (50, 300, 300, 360), field_value="no",
                  choice_values=[["sr", "Srpski"], ["no", "Norsk"], ["en", "English"]], field_flags=an.FF_MULTISELECT)
    doc.xref_set_key(xref, "DV", "[(no)(en)]")
    f = an.page_fields(page)[0]
    assert f["multi"] and f["v"] == ["no"]
    an.set_field(page, xref, ["sr", "en"])
    assert doc.xref_get_key(xref, "I") == ("array", "[0 2]")  # their places, which other readers mark
    assert an.page_fields(pymupdf.open("pdf", doc.tobytes())[0])[0]["v"] == ["sr", "en"]
    an.reset_fields(doc)
    assert an.page_fields(page)[0]["v"] == ["no", "en"] and doc.xref_get_key(xref, "I") == ("array", "[1 2]")
    an.set_field(page, xref, [])
    assert an.page_fields(page)[0]["v"] == [] and doc.xref_get_key(xref, "I")[0] == "null"


# --- annotations -------------------------------------------------------------------------------


@pytest.mark.parametrize("rot", ROTATIONS)
def test_markup_on_words(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    words = page.get_text("words")
    idx = [i for i, w in enumerate(words) if w[4] in ("lorem", "ipsum")]
    xref = an.add_markup(page, "highlight", idx, author="M")
    a = page.load_annot(xref)
    assert a.type[1] == "Highlight" and a.info["content"] == "lorem ipsum"
    r = a.rect * page.rotation_matrix
    assert r.width > r.height  # covers the words horizontally on screen
    an.add_markup(page, "underline", idx[:1])
    an.add_markup(page, "strikeout", idx[1:])
    assert {x["t"] for x in an.page_annots(page)} == {"Highlight", "Underline", "StrikeOut"}


@pytest.mark.parametrize("rot", (0, 90))
def test_ink_shapes_text_note_move_resize_delete(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    ink = an.add_ink(page, [[(100, 100), (150, 130), (200, 110)], [(120, 150)]], color="#ff0000", width=3)
    rect = an.add_shape(page, "rect", (60, 200, 160, 260), color="#0000ff", width=2)
    arrow = an.add_shape(page, "arrow", (60, 300, 200, 340))
    ft = an.add_freetext(page, (60, 380, 260, 420), "Tekst čćđ Ђ", fontsize=12)
    note = an.add_note(page, (300, 120), "Napomena")
    infos = {a["x"]: a for a in an.page_annots(page)}
    assert set(infos) == {ink, rect, arrow, ft, note}
    # the ink box sits where it was drawn on screen
    x0, y0, x1, y1 = infos[ink]["r"]
    assert 90 < x0 < 105 and 90 < y0 < 105 and 195 < x1 < 210
    for xref in (ink, rect, arrow, ft, note):
        before = infos[xref]["r"]
        an.move_annot(page, xref, 30, 20)
        after = {a["x"]: a for a in an.page_annots(page)}[xref]["r"]
        assert abs(after[0] - before[0] - 30) < 1.5 and abs(after[1] - before[1] - 20) < 1.5, (xref, before, after)
        assert abs((after[2] - after[0]) - (before[2] - before[0])) < 1.5
    for _ in range(5):  # repeated moves must not grow bordered shapes
        an.move_annot(page, rect, 1, 1)
    r = {a["x"]: a for a in an.page_annots(page)}[rect]["r"]
    assert abs((r[2] - r[0]) - (infos[rect]["r"][2] - infos[rect]["r"][0])) < 1.5
    an.resize_annot(page, ft, (100, 100, 300, 160))
    r = {a["x"]: a for a in an.page_annots(page)}[ft]["r"]
    assert abs(r[0] - 100) < 1 and abs(r[3] - 160) < 1
    an.edit_annot(page, note, content="Novi tekst", color="#22c55e")
    assert page.load_annot(note).info["content"] == "Novi tekst"
    an.delete_annot(page, ink)
    assert ink not in {a["x"] for a in an.page_annots(page)}
    with pytest.raises(ValueError):
        an.add_shape(page, "rect", (10, 10, 11, 11))


@pytest.mark.parametrize("rot", (0, 90, 270))
def test_image_stamp_upright_and_movable(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    xref = an.add_image_stamp(page, (100, 100, 300, 200), png_bytes(200, 100), author="M")
    info = {a["x"]: a for a in an.page_annots(page)}[xref]
    assert info["img"] and abs(info["r"][0] - 100) < 1 and abs(info["r"][2] - 300) < 1
    pix = page.get_pixmap(dpi=72)
    bx, by = 105, 105  # top-left of the stamp on screen must be the blue marker
    r, g, b = pix.pixel(bx, by)[:3]
    assert b > 150 and r < 100, (rot, (r, g, b))
    an.move_annot(page, xref, 50, 50)
    pix = page.get_pixmap(dpi=72)
    r, g, b = pix.pixel(bx + 50, by + 50)[:3]
    assert b > 150 and r < 100


def test_redaction_marks_and_apply():
    doc = make_doc(2)
    n = an.mark_redaction_text(doc, "lorem")
    assert n == 2 and an.count_redactions(doc) == 2
    an.mark_redaction(doc[0], (40, 50, 300, 90))
    assert an.apply_redactions(doc) == 3
    assert "lorem" not in "".join(p.get_text() for p in doc) and "Aurora sample page 1" not in doc[0].get_text()
    assert an.count_redactions(doc) == 0


def test_redaction_removes_comments_fields_and_links_under_the_mark(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Personnummer 12345678901 er hemmelig", fontsize=12)
    words, _ = an.page_words(page)
    k = next(i for i, w in enumerate(words) if w[4] == "12345678901")
    box = pymupdf.Rect(words[k][:4])
    an.add_markup(page, "highlight", [k])  # a highlight stores the words it covers
    an.add_note(page, [box.x0 + 2, box.y0 + 2], "ring 12345678901")
    an.add_note(page, [400, 400], "langt unna")  # outside the mark: stays
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "pnr", "12345678901"
    w.rect = pymupdf.Rect(box.x0, box.y1 + 4, box.x1, box.y1 + 20)
    page.add_widget(w)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": box, "uri": "https://example.com/?pnr=12345678901"})
    an.mark_redaction(page, [box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 24])
    assert an.apply_redactions(doc) == 1
    out = str(tmp_path / "redacted.pdf")
    pt.save_pdf(doc, out)
    again = pymupdf.open(out)
    assert b"12345678901" not in again.tobytes(garbage=1, deflate=False, expand=255)
    assert [a.info["content"] for a in again[0].annots()] == ["langt unna"]


@pytest.mark.parametrize("listed", (True, False))  # a pop-up belongs in /Annots too: such writers can leave it out
@pytest.mark.parametrize("rot", (0, 90))
@pytest.mark.parametrize("box", tuple(ODD_BOXES))
def test_redaction_reaches_comments_written_into_the_page_list(box, rot, listed):
    """Some writers put a comment's dictionary in /Annots itself instead of an object of its own. PyMuPDF does
    not see those, and the words in one under the mark (and in its pop-up) stayed in the redacted file."""
    doc, page = _odd_page(box, rot)
    pop = doc.get_new_xref()
    doc.update_object(pop, "<</Type/Annot/Subtype/Popup/Rect[400 600 500 700]/Contents(ring 4711)>>")
    entry = f"{pop} 0 R" if listed else ""
    doc.xref_set_key(page.xref, "Annots", f"[<</Type/Annot/Subtype/Text/Rect[205 400 225 420]/Contents(ring 4711)"
                                          f"/Popup {pop} 0 R>> {entry} <</Type/Annot/Subtype/Square"
                                          f"/Rect[220 600 300 650]/Contents(langt unna)>>]")
    doc = pymupdf.open("pdf", doc.tobytes())
    page = doc[0]
    hit = pymupdf.Rect(pt.search_rects(doc, "MARK")[0]["r"])
    an.mark_redaction(page, hit + (-4, -4, 4, 4))
    assert an.apply_redactions(doc) == 1
    out = doc.tobytes(garbage=0, deflate=False, expand=255)  # the pop-up goes even with no garbage collection
    # words, not bare digits: the file's random /ID and the offsets in its xref table can hold "4711"
    assert b"ring 4711" not in out and b"langt unna" in out and "MARK" not in pymupdf.open("pdf", out)[0].get_text()


def test_redaction_wipes_the_picture_under_the_mark(tmp_path):
    """Black on top is not enough: the picture's own pixels under the mark go, else any tool takes them out."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image((100, 100, 300, 300), stream=png_bytes(200, 200, (220, 30, 30, 255), mark=False))
    an.mark_redaction(page, (100, 100, 200, 300))  # the left half
    an.apply_redactions(doc)
    out = str(tmp_path / "r.pdf")
    pt.save_pdf(doc, out)
    saved = pymupdf.open(out)
    pix = pymupdf.Pixmap(saved, saved[0].get_images()[0][0])
    assert pix.pixel(30, 100)[:3] != (220, 30, 30) and pix.pixel(170, 100)[:3] == (220, 30, 30)


@pytest.mark.parametrize("rot", (0, 90))
def test_redaction_wipes_the_shape_a_picture_keeps_in_its_alpha(rot):
    """A signature on transparent ground keeps its shape in the picture's alpha (/SMask): under the mark that
    goes too, and the old picture and alpha leave the file."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), True)
    for y in range(40):
        for x in range(40):
            pix.set_pixel(x, y, (20, 20, 20, 255 if (x // 2 + y // 2) % 2 else 0))
    doc = pymupdf.open()
    page = doc.new_page()
    page.set_rotation(rot)
    page.insert_image((100, 100, 300, 300), stream=pix.tobytes("png"))
    an.mark_redaction(doc[0], pymupdf.Rect(100, 100, 200, 200) * doc[0].rotation_matrix)  # the top left quarter
    assert an.apply_redactions(doc) == 1
    saved = pymupdf.open("pdf", doc.tobytes(garbage=1))
    alpha = pymupdf.Pixmap(saved, saved[0].get_images(full=True)[0][1])
    assert {alpha.pixel(x, y)[0] for x in range(20) for y in range(20)} == {255}
    assert {alpha.pixel(x, y)[0] for x in range(20, 40) for y in range(20)} == {0, 255}  # the rest as it was
    assert len([x for x in range(1, saved.xref_length()) if saved.xref_get_key(x, "Subtype")[1] == "/Image"]) == 2


def test_redaction_takes_a_fields_value_with_its_last_widget(tmp_path):
    """A field's value sits in its parent object, which outlives the widgets, and a hybrid form keeps it again
    in its XFA packet: the value goes with the field's last widget, the packet with the first mark."""
    doc, af, x = _form_made_elsewhere()  # "reka" holds Морава, with a widget on page 1 and one on page 2
    xfa = doc.get_new_xref()
    doc.update_object(xfa, "<<>>")
    doc.update_stream(xfa, "<xfa:datasets><reka>Морава</reka></xfa:datasets>".encode())
    doc.xref_set_key(af, "XFA", f"{xfa} 0 R")
    reka = (45, 535, 305, 570)
    an.mark_redaction(doc[0], reka)
    assert an.apply_redactions(doc) == 1
    assert doc.xref_get_key(af, "XFA") == ("null", "null")
    assert {f["n"]: f["v"] for f in an.page_fields(doc[1])}["reka"] == "Морава"  # its other widget keeps it
    an.mark_redaction(doc[1], reka)
    assert an.apply_redactions(doc) == 1
    out = str(tmp_path / "redacted.pdf")
    pt.save_pdf(doc, out)
    saved = pymupdf.open(out)
    texts = [saved.xref_get_key(o, k)[1] for o in range(1, saved.xref_length()) for k in saved.xref_get_keys(o)]
    assert not [t for t in texts if "Морава" in t]
    assert b"041C043E0440043004320430" not in saved.tobytes(garbage=1, deflate=False, expand=255).upper()


@pytest.mark.parametrize("default", ("the choice", "another button"))
@pytest.mark.parametrize("shared", (False, True))
def test_redaction_takes_a_radio_choice_with_its_button(shared, default):
    """The group's value names the chosen button. With that button redacted, the other ones show Off and the
    value would still say which was chosen. Buttons can share one drawing of every state, so a button left
    could show the choice too: what counts is what it shows. The default (/DV) stays when it names another
    button, also when some other button goes."""
    doc = make_doc(1)
    page = doc[0]
    group, kids, faces = doc.get_new_xref(), [doc.get_new_xref() for _ in range(3)], []
    for draw in ("q 0 g 4 4 8 8 re f Q", ""):
        faces.append(doc.get_new_xref())
        doc.update_object(faces[-1], "<</Type/XObject/Subtype/Form/BBox[0 0 16 16]>>")
        doc.update_stream(faces[-1], draw.encode())
    dv = "positive" if default == "the choice" else "negative"
    doc.update_object(group, f"<</FT/Btn/Ff 49152/T(hiv)/V/positive/DV/{dv}/Kids[{' '.join(f'{k} 0 R' for k in kids)}]>>")
    names = ("positive", "negative", "unknown")
    for kid, state, y in zip(kids, names, (100, 140, 180)):
        states = "".join(f"/{n} {faces[0]} 0 R" for n in names) if shared else f"/{state} {faces[0]} 0 R"
        doc.update_object(kid, f"<</Type/Annot/Subtype/Widget/Rect[50 {y} 66 {y + 16}]/Parent {group} 0 R/F 4"
                               f"/P {page.xref} 0 R/AS/{'positive' if state == 'positive' else 'Off'}"
                               f"/AP<</N<<{states}/Off {faces[1]} 0 R>>>>>>")
    doc.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{k} 0 R" for k in kids) + "]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{group} 0 R]>>")
    page = doc.reload_page(page)
    an.mark_redaction(page, (45, 595 - 200, 75, 595 - 175))  # the "unknown" button: choice and default stay
    assert an.apply_redactions(doc) == 1
    assert [doc.xref_get_key(group, k)[1] for k in ("V", "DV")] == ["/positive", f"/{dv}"]
    an.mark_redaction(doc[0], (45, 595 - 120, 75, 595 - 95))  # the chosen button
    assert an.apply_redactions(doc) == 1
    assert [doc.xref_get_key(group, k)[1] for k in ("V", "DV")] == ["null", "null" if dv == "positive" else "/negative"]
    assert doc.xref_get_key(group, "T") == ("string", "hiv")  # the group and its other button stay


def test_redaction_keeps_a_field_that_a_redacted_widget_heads():
    """Some form tools make a widget the field of widgets on other pages too (/T, /V and /Kids in one object):
    emptying it with the widget took the field from the other pages."""
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    w1, w2 = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(w1, f"<</Type/Annot/Subtype/Widget/FT/Tx/T(navn)/V(Ola Nordmann)/DA(/Helv 12 Tf 0 g)"
                          f"/Rect[50 700 250 720]/F 4/P {doc.page_xref(0)} 0 R/Kids[{w2} 0 R]>>")
    doc.update_object(w2, f"<</Type/Annot/Subtype/Widget/Rect[50 700 250 720]/F 4/P {doc.page_xref(1)} 0 R/Parent {w1} 0 R>>")
    doc.xref_set_key(doc.page_xref(0), "Annots", f"[{w1} 0 R]")
    doc.xref_set_key(doc.page_xref(1), "Annots", f"[{w2} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{w1} 0 R]>>")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (45, 842 - 725, 255, 842 - 695))
    assert an.apply_redactions(doc) == 1
    assert [(f["n"], f["v"]) for f in an.page_fields(doc[1])] == [("navn", "Ola Nordmann")]


def test_redaction_reaches_what_a_tagged_file_says_again(tmp_path):
    """A tagged (accessible) file says its words again: in the /ActualText of its marked content, which copying
    uses, in the /Alt and /ActualText of its structure elements, and its structure tree points at the comments
    and fields, which kept their objects in the file after they left the page. On the page that was not
    redacted the words for screen readers stay."""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page()
    font = "<</Font<</F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>"
    for pno, text in ((0, "Personnummer 12345678901"), (1, "Kart over Oslo")):
        c = doc.get_new_xref()
        doc.update_object(c, "<<>>")
        doc.update_stream(c, f"/Span <</MCID 0 /ActualText ({text})>> BDC BT /F9 12 Tf 72 700 Td ({text}) Tj ET EMC".encode())
        doc.xref_set_key(doc.page_xref(pno), "Contents", f"{c} 0 R")
        doc.xref_set_key(doc.page_xref(pno), "Resources", font)
    page = doc[0]
    words, _ = an.page_words(page)
    k = next(i for i, w in enumerate(words) if w[4] == "12345678901")
    box = pymupdf.Rect(words[k][:4])
    hx = an.add_markup(page, "highlight", [k])  # stores the words in /Contents
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "pnr", "12345678901"
    w.rect = pymupdf.Rect(box.x0, box.y1 + 4, box.x1 + 60, box.y1 + 20)
    wx = page.add_widget(w).xref
    elems = []
    for pno, alt, obj in ((0, "nummer 12345678901", None), (1, "Kart over Oslo", None), (0, "", hx), (0, "", wx)):
        se = doc.get_new_xref()
        kid = f"<</Type/OBJR/Obj {obj} 0 R>>" if obj else "0"
        doc.update_object(se, f"<</Type/StructElem/S/P/Pg {doc.page_xref(pno)} 0 R/Alt({alt})/ActualText({alt})/K {kid}>>")
        elems.append(f"{se} 0 R")
    root = doc.get_new_xref()
    doc.update_object(root, f"<</Type/StructTreeRoot/K[{' '.join(elems)}]>>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], [box.x0 - 2, box.y0 - 2, box.x1 + 62, box.y1 + 24])
    assert an.apply_redactions(doc) == 1
    out = str(tmp_path / "r.pdf")
    pt.save_pdf(doc, out)
    saved = pymupdf.open(out)
    assert "12345678901" not in saved[0].get_text()
    assert b"12345678901" not in saved.tobytes(garbage=1, deflate=False, expand=255)
    assert b"Kart over Oslo" in saved.tobytes(garbage=1, deflate=False, expand=255)
    assert "Kart over Oslo" in saved[1].get_text()


HELV = "/Font<</F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>"


def _tagged(rot=0, form=False, hidden=False, picture=False):
    """A tagged page: paragraph 0 says a secret (inside a drawing with ``form``), paragraph 1 does not. Words are
    said again in marked content (/ActualText inline and in a named list), in the structure elements and around
    them; ``hidden`` adds a layer that is off, ``picture`` an inline picture under the mark."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    p0 = b"/P <</MCID 0>> BDC BT /F9 12 Tf 72 700 Td (Personnummer 12345678901) Tj ET EMC "
    xo = ""
    if form:
        fx = doc.get_new_xref()
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>/StructParents 1>>")
        doc.update_stream(fx, b"/P <</MCID 0 /ActualText (Personnummer 12345678901)>> BDC BT /F9 12 Tf 72 700 Td "
                              b"(Personnummer 12345678901) Tj ET EMC")
        p0, xo = b"/Fm0 Do ", f"/XObject<</Fm0 {fx} 0 R>>"
    if hidden:
        p0 += b"/OC /oc1 BDC /Span <</ActualText (skjult)>> BDC BT /F9 12 Tf 72 400 Td (layer) Tj ET EMC EMC "
    if picture:
        p0 += b"q 10 0 0 10 200 700 cm BI /W 2 /H 1 /BPC 8 /CS /G ID \x00\x45\x49\x20 EI Q "
    c = doc.get_new_xref()
    doc.update_object(c, "<<>>")
    doc.update_stream(c, p0 + b"/P <</MCID 1>> BDC BT /F9 12 Tf 72 500 Td (Kart over Oslo) Tj "
                              b"/Span <</ActualText (fi)>> BDC (fi) Tj EMC ET EMC "
                              b"/Span /PX BDC BT /F9 12 Tf 72 300 Td (named) Tj ET EMC")
    doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    oc = ""
    if hidden:
        ocg = doc.get_new_xref()
        doc.update_object(ocg, "<</Type/OCG/Name(skjult)>>")
        doc.xref_set_key(doc.pdf_catalog(), "OCProperties", f"<</OCGs[{ocg} 0 R]/D<</OFF[{ocg} 0 R]>>>>")
        oc = f"/oc1 {ocg} 0 R"
    doc.xref_set_key(page.xref, "Resources", f"<<{HELV}{xo}/Properties<</PX<</ActualText(named prop)>>{oc}>>>>")
    doc.xref_set_key(page.xref, "StructParents", "0")
    root, e0, e1, top = (doc.get_new_xref() for _ in range(4))
    k0 = f"<</Type/MCR/Pg {page.xref} 0 R/Stm {fx} 0 R/MCID 0>>" if form else "0"
    doc.update_object(e0, f"<</Type/StructElem/S/P/P {top} 0 R/Pg {page.xref} 0 R/K {k0}/Alt(alt 12345678901)"
                          f"/ActualText(nummer 12345678901)>>")
    doc.update_object(e1, f"<</Type/StructElem/S/P/P {top} 0 R/Pg {page.xref} 0 R/K 1/Alt(Kart alt)"
                          f"/ActualText(Kart over Oslo)>>")
    doc.update_object(top, f"<</Type/StructElem/S/Document/P {root} 0 R/K[{e0} 0 R {e1} 0 R]/Alt(doc 12345678901)>>")
    nums = f"0 [null {e1} 0 R] 1 [{e0} 0 R]" if form else f"0 [{e0} 0 R {e1} 0 R]"
    doc.update_object(root, f"<</Type/StructTreeRoot/K {top} 0 R/ParentTree<</Nums[{nums}]>>>>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    doc[0].set_rotation(rot)
    return doc


def _redact_secret(doc, pno=0):
    box = pymupdf.Rect(next(r["r"] for r in pt.search_rects(doc, "12345678901") if r["p"] == pno))
    an.mark_redaction(doc[pno], box + (-2, -2, 2, 2))
    assert an.apply_redactions(doc) == 1
    return doc.tobytes(garbage=1, deflate=False, expand=255)


@pytest.mark.parametrize("case", (dict(), dict(rot=90), dict(form=True), dict(rot=270, form=True), dict(hidden=True),
                                  dict(picture=True)))
def test_redaction_takes_only_the_tagged_words_under_the_mark(case):
    """A tagged page says its words again, for copying and screen readers. What it says for content under a mark
    goes, also in the elements around it; what it says for the rest of the page stays. The tree points at a
    drawing by object, and pointed at the original after MuPDF drew the page from a copy: the original, with the
    words under the mark, stayed in the file. A hidden layer stays hidden."""
    doc = _tagged(**case)
    raw = _redact_secret(doc)
    assert b"12345678901" not in raw
    for kept in (b"Kart over Oslo", b"Kart alt", b"/ActualText (fi)", b"(named prop)"):
        assert kept in raw, kept
    assert b"aurora-" not in raw and b"AuroraCopyOf" not in raw
    words = doc[0].get_text().split()
    assert "Kart" in words and "prop" in words and "layer" not in words and "skjult" not in words
    if case.get("form"):
        mcr = next(x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "K/Type")[1] == "/MCR")
        assert int(doc.xref_get_key(mcr, "K/Stm")[1].split()[0]) == doc.get_page_xobjects(0)[0][0]


def test_redaction_leaves_drawings_and_lists_that_other_pages_share():
    """Two pages draw one drawing and name one property list. Redacting page 1 takes the words from what page 1
    draws; page 2 still says them."""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=612, height=792)
    fx, plist, res = doc.get_new_xref(), doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
    doc.update_stream(fx, b"/Span <</ActualText (felles 4711)>> BDC BT /F9 12 Tf 72 700 Td (felles) Tj ET EMC")
    doc.update_object(plist, "<</ActualText (navn 4711)>>")
    doc.update_object(res, f"<<{HELV}/XObject<</Fm0 {fx} 0 R>>/Properties<</PX {plist} 0 R>>>>")
    for pno in range(2):
        c = doc.get_new_xref()
        doc.update_object(c, "<<>>")
        doc.update_stream(c, b"/Fm0 Do /Span /PX BDC BT /F9 12 Tf 72 680 Td (navn) Tj ET EMC")
        doc.xref_set_key(doc.page_xref(pno), "Contents", f"{c} 0 R")
        doc.xref_set_key(doc.page_xref(pno), "Resources", f"{res} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (60, 80, 200, 120))
    assert an.apply_redactions(doc) == 1
    assert "4711" not in doc[0].get_text() and doc[1].get_text().split() == ["felles", "4711", "navn", "4711"]
    raw = doc.tobytes(garbage=1, deflate=False, expand=255)
    # once in the drawing, once in the list: page 2's
    assert raw.count(b"felles 4711") == 1 and raw.count(b"navn 4711") == 1 and b"aurora-" not in raw


def test_redaction_takes_a_tagged_picture_from_the_tree():
    """The tree can point at a picture by object. MuPDF draws a picture under a mark from a new one without the
    pixels, and the tree kept the old one, with all its pixels, in the file. The words for the other picture
    stay."""
    doc = pymupdf.open()
    page = doc.new_page()
    for r, color in (((100, 100, 200, 200), (220, 30, 30, 255)), ((300, 100, 400, 200), (30, 30, 220, 255))):
        page.insert_image(r, stream=png_bytes(40, 40, color, mark=False))
    pics = [x[0] for x in page.get_images(full=True)]
    assert len(set(pics)) == 2
    root, figs = doc.get_new_xref(), [doc.get_new_xref() for _ in pics]
    for fig, pic, alt in zip(figs, pics, ("bilde 4711", "bilde langt unna")):
        doc.update_object(fig, f"<</Type/StructElem/S/Figure/P {root} 0 R/Pg {page.xref} 0 R/Alt({alt})"
                               f"/K<</Type/OBJR/Obj {pic} 0 R>>>>")
    doc.update_object(root, f"<</Type/StructTreeRoot/K[{' '.join(f'{f} 0 R' for f in figs)}]>>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (100, 100, 150, 200))  # half of the first picture
    assert an.apply_redactions(doc) == 1
    saved = pymupdf.open("pdf", doc.tobytes(garbage=1))
    raw = saved.tobytes(garbage=1, deflate=False, expand=255)
    assert b"bilde 4711" not in raw and b"bilde langt unna" in raw
    pictures = {x for x in range(1, saved.xref_length()) if saved.xref_get_key(x, "Subtype")[1] == "/Image"}
    assert pictures == {x for img in saved[0].get_images(full=True) for x in img[:2] if x}  # and their masks


def test_redaction_takes_a_tagged_comments_drawing_from_the_tree():
    """The tree can point into the drawing of a comment (/Stm with /StmOwn). With the comment under a mark gone,
    that pointer kept its drawing, words and all, in the file."""
    doc = pymupdf.open()
    page = doc.new_page()
    a = page.add_freetext_annot((100, 100, 300, 140), "ring 4711", fontsize=12)
    a.update()
    ap = int(doc.xref_get_key(a.xref, "AP/N")[1].split()[0])
    doc.update_stream(ap, b"/P <</MCID 0>> BDC " + doc.xref_stream(ap) + b" EMC")
    root, elem = doc.get_new_xref(), doc.get_new_xref()
    mcr = f"<</Type/MCR/Stm {ap} 0 R/StmOwn {a.xref} 0 R/MCID 0>>"
    doc.update_object(elem, f"<</Type/StructElem/S/P/P {root} 0 R/Pg {page.xref} 0 R/Alt(ring 4711)"
                            f"/K[{mcr} <</Type/OBJR/Obj {a.xref} 0 R>>]>>")
    doc.update_object(root, f"<</Type/StructTreeRoot/K {elem} 0 R>>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (90, 90, 310, 150))
    assert an.apply_redactions(doc) == 1
    assert b"ring 4711" not in doc.tobytes(garbage=1, deflate=False, expand=255)


@pytest.mark.parametrize("src, want", (
    (b"/S <</MCID 0 /ActualText (a (nested) \\) one)>> BDC BT (x) Tj ET EMC", b"/S <</MCID 0  >> BDC BT (x) Tj ET EMC"),
    (b"/P <</Lang (nb) /E (fork.) /Alt <FEFF0041>>> BDC EMC", b"/P <</Lang (nb)    >> BDC EMC"),
    (b"/X <</Type /E /A <</O /Layout /Alt (z)>>>> BDC EMC", b"/X <</Type /E /A <</O /Layout  >>>> BDC EMC"),  # a value /E stays
    (b"BT (/ActualText (x) /Alt) Tj [(a) 2 (/Alt) -3] TJ ET", b"BT (/ActualText (x) /Alt) Tj [(a) 2 (/Alt) -3] TJ ET"),
    (b"BI /W 2 /H 1 /BPC 8 /CS /G ID \x00<</Alt(x)>> EI /S <</Alt(y)>> BDC", b"BI /W 2 /H 1 /BPC 8 /CS /G ID \x00<</Alt(x)>> EI /S << >> BDC"),
    (b"BI /W 1 /H 1 /BPC 8 /CS /G ID\t<</Alt(x)>> EI", b"BI /W 1 /H 1 /BPC 8 /CS /G ID\t<</Alt(x)>> EI"),
    (b"/S <</MCID 0 /Actual#54ext (x)>> BDC EMC", b"/S <</MCID 0  >> BDC EMC"),  # a key written with #xx
))
def test_redaction_cleans_only_the_words_of_marked_content(src, want):
    """Only the keys go, with their values: text, arrays, picture data and other entries stay byte for byte."""
    assert an._strip_alt(src) == want


SECRET = b"/P <</MCID 0 /ActualText (SECRET-ALT)>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC "
KEEP = b" /P <</MCID 1 /ActualText (keep-alt)>> BDC BT /F9 12 Tf 72 500 Td (KEEPME) Tj ET EMC"


def _page_of(content: bytes, res: str = "", doc=None, w=612, h=792):
    """A page that draws ``content`` (several streams for a list), with Helvetica as /F9 and resources ``res``."""
    doc = doc if doc is not None else pymupdf.open()
    page = doc.new_page(width=w, height=h)
    refs = []
    for part in content if isinstance(content, list) else [content]:
        c = doc.get_new_xref()
        doc.update_object(c, "<<>>")
        doc.update_stream(c, part)
        refs.append(f"{c} 0 R")
    doc.xref_set_key(page.xref, "Contents", f"[{' '.join(refs)}]")
    doc.xref_set_key(page.xref, "Resources", f"<<{HELV}{res}>>")
    return doc


def _raw(doc) -> bytes:
    return doc.tobytes(garbage=1, deflate=False, expand=255)


def _layer_doc(tag: bytes):
    doc = _page_of(tag + b" BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC" + KEEP)
    ocg = doc.get_new_xref()
    doc.update_object(ocg, "<</Type/OCG/Name(lag)/ActualText(SECRET-ALT)>>")
    doc.xref_set_key(doc.pdf_catalog(), "OCProperties", f"<</OCGs[{ocg} 0 R]/D<</ON[{ocg} 0 R]>>>>")
    doc.xref_set_key(doc.page_xref(0), "Resources", f"<<{HELV}/Properties<</X {ocg} 0 R>>>>")
    return doc


@pytest.mark.parametrize("make", (
    lambda: _page_of(b"/P <</MCID 0 % a note >>\n/ActualText (SECRET-ALT)>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET "
                     b"EMC" + KEEP),  # a comment in the list
    lambda: _page_of(b"q 10 0 0 10 300 300 cm BI /W 1 /H 1 /BPC 8 /CS /G /F /AHx ID 80>EI Q " + SECRET + KEEP),
    lambda: _page_of(b"q 20 0 0 20 300 300 cm BI /W 6 /H 1 /BPC 8 /CS /G ID \x20EI\x20(A EI Q " + SECRET + KEEP),
    lambda: _page_of([b"/P <</MCID 0", SECRET[13:] + KEEP]),  # the list goes on in the next stream
    lambda: _layer_doc(b"/OC /X BDC"),  # a layer that says words, as layer and as list
    lambda: _layer_doc(b"/Span /X BDC"),
    lambda: _page_of(b"/P <</MCID 0 /A [1 % ] (\n 2] /ActualText (SECRET-ALT)>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj "
                     b"ET EMC" + KEEP),  # a comment in an array of the list
), ids=("comment in the list", "hex picture before EI", "picture bytes reading EI", "list over two streams",
        "layer", "layer as list", "comment in an array"))
def test_redaction_takes_the_words_of_content_read_another_way(make):
    """Marked content this program reads otherwise than MuPDF does: a comment holding >> in its list, or ] ( in
    an array there, an inline picture whose EI comes right after the data or whose bytes read EI, a list that goes
    on in the next content stream, and a layer that says words. The words of what was under the mark stayed in the
    file."""
    doc = pymupdf.open("pdf", make().tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    raw = _raw(doc)
    assert b"SECRET" not in raw and b"KEEPME" in raw
    assert b"aurora-" not in raw and b"AuroraCopyOf" not in raw


def test_redaction_reaches_the_quads_of_a_mark_made_elsewhere():
    """A mark made in another program can have /QuadPoints away from its /Rect: MuPDF takes what is under the
    quads, and the words said for it stayed."""
    doc = _page_of(SECRET + KEEP)
    a = doc[0].add_redact_annot((300, 300, 320, 320))
    doc.xref_set_key(a.xref, "QuadPoints", "[70 715 130 715 70 694 130 694]")  # over SECRET1, in PDF space
    doc = pymupdf.open("pdf", doc.tobytes())
    assert an.apply_redactions(doc) == 1
    raw = _raw(doc)
    assert b"SECRET" not in raw and b"keep-alt" in raw


@pytest.mark.parametrize("tree", (False, True))
def test_redaction_takes_the_words_of_glyphs_taken_off_their_ink(tree):
    """MuPDF takes a glyph whose box touches the mark, also where its ink does not (a mark drawn a little too
    high): the words said for it stayed, in its marked content and in the structure tree."""
    if tree:
        doc = _page_of(b"/Span <</MCID 0>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC "
                       b"/P <</MCID 1>> BDC BT /F9 12 Tf 72 600 Td (Kart over Oslo) Tj ET EMC")
        root, e0, e1 = (doc.get_new_xref() for _ in range(3))
        doc.update_object(e0, f"<</Type/StructElem/S/Span/P {root} 0 R/Pg {doc.page_xref(0)} 0 R/K 0"
                              f"/ActualText(SECRET1)/Alt(SECRET-ALT)>>")
        doc.update_object(e1, f"<</Type/StructElem/S/P/P {root} 0 R/Pg {doc.page_xref(0)} 0 R/K 1"
                              f"/ActualText(keep-alt)>>")
        doc.update_object(root, f"<</Type/StructTreeRoot/K[{e0} 0 R {e1} 0 R]"
                                f"/ParentTree<</Nums[0 [{e0} 0 R {e1} 0 R]]>>>>")
        doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
        doc.xref_set_key(doc.page_xref(0), "StructParents", "0")
        glyphs, mark = b"(SECRET1)", (72, 77, 130, 81)  # the ink of the capitals starts at y 82.3
    else:
        doc = _page_of(b"/Span <</ActualText (SECRET-ALT)>> BDC BT /F9 40 Tf 100 600 Td (aaaa) Tj ET EMC" + KEEP)
        glyphs, mark = b"(aaaa)", (100, 153, 190, 157)  # above the ink of the a's, inside the font's box
    doc = pymupdf.open("pdf", doc.tobytes())
    pix = doc[0].get_pixmap(dpi=72, alpha=False)
    assert not any(sum(pix.pixel(x, y)) < 600 for y in range(mark[1], mark[3]) for x in range(mark[0], mark[2]))
    an.mark_redaction(doc[0], mark)
    assert an.apply_redactions(doc) == 1
    raw = _raw(doc)
    assert glyphs not in raw and b"SECRET" not in raw and b"keep-alt" in raw


def test_redaction_reads_on_past_a_drawing_without_data():
    """A drawing that is a dictionary without a stream (MuPDF skips it) stopped the look at the page before it
    met the marked content in the other drawing, and its words stayed."""
    doc = pymupdf.open()
    good, bad = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(good, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
    doc.update_stream(good, SECRET)
    doc.update_object(bad, "<</Type/XObject/Subtype/Form/BBox[0 0 10 10]>>")
    _page_of(b"/Good Do /Bad Do", f"/XObject<</Good {good} 0 R/Bad {bad} 0 R>>", doc)  # no marked content of its own
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert b"SECRET" not in _raw(doc)


def _png_up(data: bytes, columns: int) -> bytes:
    """``data`` as rows for the PNG Up predictor, which a /DecodeParms <</Predictor 12>> reads back."""
    data += b" " * (-len(data) % columns)
    out, prev = [], bytes(columns)
    for i in range(0, len(data), columns):
        row = data[i:i + columns]
        out.append(b"\x02" + bytes((a - b) & 0xFF for a, b in zip(row, prev)))
        prev = row
    return b"".join(out)


def test_redaction_keeps_streams_compressed_with_a_predictor():
    """A content stream and a drawing two pages share, compressed with a PNG predictor: written back with the
    predictor still named, the stream read as garbage, and the redaction wiped the page, and page 2 lost the
    drawing."""
    import zlib

    doc = pymupdf.open()
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
    for _ in range(2):
        _page_of(b"/P <</MCID 0>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC /P <</MCID 1>> BDC BT /F9 12 Tf "
                 b"72 500 Td (Text that must stay) Tj ET EMC /Fm0 Do", f"/XObject<</Fm0 {fx} 0 R>>", doc)
    for xref, data in ((fx, b"/P <</MCID 0>> BDC BT /F9 12 Tf 72 300 Td (Shared drawing) Tj ET EMC"),
                       *((doc[p].get_contents()[0], doc.xref_stream(doc[p].get_contents()[0])) for p in range(2))):
        doc.update_stream(xref, zlib.compress(_png_up(data, 80)), compress=False)
        doc.xref_set_key(xref, "Filter", "/FlateDecode")
        doc.xref_set_key(xref, "DecodeParms", "<</Predictor 12/Columns 80>>")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], doc[0].search_for("SECRET1")[0] + (-2, -2, 2, 2))
    assert an.apply_redactions(doc) == 1
    saved = pymupdf.open("pdf", doc.tobytes(garbage=1))
    assert saved[0].get_text().split("\n")[:2] == ["Text that must stay", "Shared drawing"]
    assert saved[1].get_text().split("\n")[:3] == ["SECRET1", "Text that must stay", "Shared drawing"]


@pytest.mark.parametrize("what", ("drawing", "picture", "list"))
def test_redaction_takes_originals_the_page_tree_hands_down(what):
    """Resources the page tree hands down to its pages (as some scanners write them) still named the original
    drawing or picture after MuPDF drew the page from a new one, which kept what was under the mark in the file;
    a property list named there kept its words."""
    doc = pymupdf.open()
    if what == "picture":
        page = doc.new_page(width=200, height=200)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
        pix.set_rect(pix.irect, (220, 30, 30))
        page.insert_image((0, 0, 200, 200), pixmap=pix)
        res = doc.xref_get_key(page.xref, "Resources")[1]
    else:
        fx, plist = doc.get_new_xref(), doc.get_new_xref()
        doc.update_object(plist, "<</ActualText(SECRET-ALT)>>")
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
        doc.update_stream(fx, b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET" if what == "drawing" else
                          b"/Span /PX BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC")
        _page_of(b"/Fm0 Do" + KEEP, doc=doc)
        res = f"<<{HELV}/XObject<</Fm0 {fx} 0 R>>/Properties<</PX {plist} 0 R>>>>"
        if what == "list":
            doc.xref_set_key(fx, "Resources", "null")  # the drawing reads the page's resources
    doc.xref_set_key(doc.page_xref(0), "Resources", "null")
    doc.xref_set_key(int(doc.xref_get_key(doc.pdf_catalog(), "Pages")[1].split()[0]), "Resources", res)
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (0, 0, 100, 200) if what == "picture" else (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    saved = pymupdf.open("pdf", doc.tobytes(garbage=1))
    raw = _raw(saved)
    assert b"SECRET" not in raw
    if what == "picture":
        pictures = [x for x in range(1, saved.xref_length()) if saved.xref_get_key(x, "Subtype")[1] == "/Image"]
        assert pictures == [img[0] for img in saved[0].get_images(full=True)]
        assert pymupdf.Pixmap(saved, pictures[0]).pixel(2, 10) == (255, 255, 255)
    else:
        assert b"KEEPME" in raw


def _tiling(doc, content: bytes, res: str = "") -> int:
    pat = doc.get_new_xref()
    doc.update_object(pat, f"<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 612 792]/XStep 612"
                           f"/YStep 792/Resources<<{HELV}{res}>>>>")
    doc.update_stream(pat, content)
    return pat


FILL = b"q /Pattern cs /P0 scn 0 0 612 792 re f Q "
MASKED = b"q /GS0 gs 0 0 1 rg 0 0 612 792 re f Q "  # blue where the soft mask is light
PATTERN_TEXT = b"/Span <</ActualText (SECRET-ALT)>> BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC"


def _mask_group(doc, content: bytes, res: str = "") -> str:
    """Resources naming an ExtGState (/GS0) whose soft mask draws ``content`` (light: what is drawn under it
    shows)."""
    g, gs = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(g, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Group<</S/Transparency/CS/DeviceGray>>"
                         f"/Resources<<{HELV}{res}>>>>")
    doc.update_stream(g, content)
    doc.update_object(gs, f"<</Type/ExtGState/SMask<</S/Luminosity/G {g} 0 R>>>>")
    return f"/ExtGState<</GS0 {gs} 0 R>>"


def _patterned(case: str):
    """Two pages that draw the same, with SECRET-ALT drawn through a pattern or a soft mask as ``case`` says, and
    KEEPME."""
    doc = pymupdf.open()
    keep = b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj ET"
    if case == "soft mask":  # with a drawing of its own beside the text
        content, res = MASKED + keep, _mask_group(doc, b"1 g 10 10 50 50 re f " + PATTERN_TEXT)
    elif case.startswith("drawing shared with a soft mask"):  # the mask draws it away from the mark
        fx = doc.get_new_xref()
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
        doc.update_stream(fx, PATTERN_TEXT)
        mask = _mask_group(doc, b"1 g q 1 0 0 1 0 -400 cm /Fm0 Do Q", f"/XObject<</Fm0 {fx} 0 R>>")
        if case.endswith("in a pattern"):  # the mask is set in a pattern that fills an area away from the mark
            content, mask = b"/Fm0 Do q /Pattern cs /P0 scn 0 0 612 300 re f Q " + keep, \
                f"/Pattern<</P0 {_tiling(doc, MASKED, mask)} 0 R>>"
        else:
            content = b"/Fm0 Do " + MASKED + keep
        res = f"/XObject<</Fm0 {fx} 0 R>>" + mask
    elif case == "pattern":  # with a drawing of its own beside the text
        pat = _tiling(doc, b"0 0 1 rg 10 10 50 50 re f " + PATTERN_TEXT)
        content, res = FILL + keep, f"/Pattern<</P0 {pat} 0 R>>"
    elif case == "in a drawing":
        fx, pat = doc.get_new_xref(), _tiling(doc, PATTERN_TEXT)
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]"
                              f"/Resources<</Pattern<</P0 {pat} 0 R>>>>>>")
        doc.update_stream(fx, FILL)
        content, res = b"/Fm0 Do " + keep, f"/XObject<</Fm0 {fx} 0 R>>"
    elif case == "in a pattern":
        inner = _tiling(doc, PATTERN_TEXT)
        outer = _tiling(doc, FILL.replace(b"/P0", b"/P1"), f"/Pattern<</P1 {inner} 0 R>>")
        content, res = FILL + keep, f"/Pattern<</P0 {outer} 0 R>>"
    else:  # one drawing drawn by the page and in a pattern that fills an area away from the mark
        fx = doc.get_new_xref()
        doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
        doc.update_stream(fx, PATTERN_TEXT)
        pat = _tiling(doc, b"/Fm0 Do", f"/XObject<</Fm0 {fx} 0 R>>")
        content = b"/Fm0 Do q /Pattern cs /P0 scn 0 0 612 300 re f Q " + keep
        res = f"/XObject<</Fm0 {fx} 0 R>>/Pattern<</P0 {pat} 0 R>>"
    for _ in range(2):
        _page_of(content, res, doc)
    return pymupdf.open("pdf", doc.tobytes())


@pytest.mark.parametrize("case", ("pattern", "in a drawing", "in a pattern", "drawing shared with a pattern",
                                  "soft mask", "drawing shared with a soft mask",
                                  "drawing shared with a soft mask in a pattern"))
def test_redaction_reaches_text_in_patterns_and_soft_masks(case):
    """MuPDF's redaction leaves what a page fills an area with through a tiling pattern, and what a soft mask
    draws: text drawn there stayed under the mark, copied and read aloud, and a pattern or mask that draws a
    drawing the page draws too kept the original, words and all. Page 2 draws the same and keeps it; the
    pattern's and the mask's own drawing stays."""
    doc = _patterned(case)
    page2 = doc[1].get_text()
    assert "SECRET-ALT" in page2
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    saved = pymupdf.open("pdf", doc.tobytes(garbage=1))
    assert "SECRET" not in saved[0].get_text() and "KEEPME" in saved[0].get_text()
    one = pymupdf.open()  # what page 1 holds, without page 2
    one.insert_pdf(saved, from_page=0, to_page=0)
    assert b"SECRET" not in _raw(one) and b"KEEPME" in _raw(one)
    assert saved[1].get_text() == page2
    if case in ("pattern", "soft mask"):
        assert saved[0].get_pixmap(dpi=72).pixel(30, 792 - 35) == (0, 0, 255)


@pytest.mark.parametrize("case", ("pattern", "soft mask"))
def test_redaction_reaches_text_in_a_layer_that_is_off(case):
    """A layer that is off hides its content from the page as shown, not from MuPDF's redaction, which writes it
    out anew: the check for text left under the mark read the page as shown, and the text a pattern or a soft mask
    draws there stayed in the file, to show when the layer is turned on."""
    doc = pymupdf.open()
    ocg = doc.add_ocg("lag", on=False)
    draw, res = (FILL, f"/Pattern<</P0 {_tiling(doc, PATTERN_TEXT)} 0 R>>") if case == "pattern" else \
        (MASKED, _mask_group(doc, PATTERN_TEXT))
    doc = pymupdf.open("pdf", _page_of(b"/OC /X BDC " + draw + b"EMC" + KEEP, f"/Properties<</X {ocg} 0 R>>{res}",
                                       doc).tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert b"SECRET" not in _raw(doc) and b"KEEPME" in _raw(doc)


def test_redaction_checks_text_in_a_layer_that_is_off(monkeypatch):
    """The check after the redaction read the page as shown too, and let text in a layer that is off move (here
    MuPDF's rewrite, without our help against it, puts the second KEEPME after the first)."""
    monkeypatch.setattr(an, "_steady", lambda data, *args, **kw: data)
    doc = pymupdf.open()
    ocg = doc.add_ocg("lag", on=False)
    doc = pymupdf.open("pdf", _page_of(SECRET + b"/OC /X BDC " + STEADY_CASES["0 0 Td"][0] + b" EMC",
                                       f"/Properties<</X {ocg} 0 R>>", doc).tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


@pytest.mark.parametrize("tree", (False, True))
def test_redaction_takes_the_words_of_marked_content_around_a_layer_that_is_off(tree):
    """Marked content that says words of its own (an /ActualText, or a structure element by its number) around a
    layer that is off, with the word under the mark in that layer: MuPDF's redaction takes the word, but the look at
    what each marked content draws read the page as shown, missed nothing, and gave the words back."""
    doc = pymupdf.open()
    ocg = doc.add_ocg("lag", on=False)
    head = b"/Span <</MCID 0>> BDC " if tree else b"/Span <</ActualText (SECRET-ALT)>> BDC "
    _page_of(head + b"/OC /X BDC BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET EMC BT /F9 12 Tf 72 640 Td (and more) Tj ET "
             b"EMC" + KEEP, f"/Properties<</X {ocg} 0 R>>", doc)
    if tree:
        root, elem = doc.get_new_xref(), doc.get_new_xref()
        doc.update_object(elem, f"<</Type/StructElem/S/Span/P {root} 0 R/Pg {doc.page_xref(0)} 0 R/K 0"
                                f"/ActualText(SECRET1 and more)>>")  # MuPDF takes SECRET-ALT itself, not this
        doc.update_object(root, f"<</Type/StructTreeRoot/K[{elem} 0 R]/ParentTree<</Nums[0 [{elem} 0 R]]>>>>")
        doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")
        doc.xref_set_key(doc.page_xref(0), "StructParents", "0")
    doc = pymupdf.open("pdf", doc.tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    raw = _raw(doc)
    assert b"SECRET" not in raw and b"keep-alt" in raw


@pytest.mark.parametrize("rot", (90, 180, 270))
def test_redaction_reaches_text_in_a_pattern_on_a_turned_page_with_a_user_unit(rot):
    """Marks are read through page.derotation_matrix, which turns the page about its CropBox as written, get_text
    about its size as MuPDF draws it: with a /UserUnit the two lie apart on a turned page, the check for text left
    under the mark looked elsewhere, and the pattern's SECRET1 stayed in the file."""
    doc = pymupdf.open()
    doc = _page_of(FILL + KEEP, f"/Pattern<</P0 {_tiling(doc, PATTERN_TEXT)} 0 R>>", doc)
    doc.xref_set_key(doc.page_xref(0), "UserUnit", "2")
    doc = pymupdf.open("pdf", doc.tobytes())
    doc[0].set_rotation(rot)
    an.mark_redaction(doc[0], tuple(pymupdf.Rect(pt.search_rects(doc, "SECRET-ALT")[0]["r"]) + (-2, -2, 2, 2)))
    assert an.apply_redactions(doc) == 1
    assert b"SECRET" not in _raw(doc) and b"KEEPME" in _raw(doc)


@pytest.mark.parametrize("case", ("pattern", "soft mask"))
def test_redaction_stops_when_a_glyph_stays_under_the_mark(monkeypatch, case):
    """A glyph still under a mark after all of it (here: a copy that kept its text) stops the redaction with a
    message, rather than leave it there unsaid."""
    monkeypatch.setattr(an, "_without_text", lambda doc, xref, *_: xref)
    doc = _patterned(case)
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    with pytest.raises(ValueError, match="REDACT_LEFT"):
        an.apply_redactions(doc)


def test_redaction_of_text_that_clips_in_a_pattern_shows_nothing_there():
    """Text in a pattern can clip (render mode 7): what is drawn after it shows inside the letters only. Without
    its text the copy clips all of that away, as MuPDF's redaction does: a text object without text leaves the
    clip as it was, and the blue behind the letters would have filled the page."""
    pat = b"q 7 Tr BT /F9 40 Tf 72 700 Td (SECRET1) Tj ET 0 0 1 rg 0 0 612 792 re f Q 1 0 0 rg 10 10 50 50 re f"
    doc = pymupdf.open()
    doc = pymupdf.open("pdf", _page_of(FILL + b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj ET",
                                       f"/Pattern<</P0 {_tiling(doc, pat)} 0 R>>", doc).tobytes())
    assert doc[0].get_pixmap(dpi=72).pixel(92, 80) == (0, 0, 255)  # in the S
    an.mark_redaction(doc[0], (60, 50, 400, 110))
    assert an.apply_redactions(doc) == 1
    pix = doc[0].get_pixmap(dpi=72)
    assert doc[0].get_text().split() == ["KEEPME"]
    assert pix.pixel(300, 400) == (255, 255, 255) and pix.pixel(30, 792 - 35) == (255, 0, 0)  # and the fill: black


@pytest.mark.parametrize("src, want", (
    (b"BT /F9 12 Tf 72 700 Td (a) Tj [(b) -20 (c)] TJ ET", b"BT /F9 12 Tf 72 700 Td   ET"),
    (b"BT 14 TL (a) ' 1 2 (b) \" ET 1 0 0 RG", b"BT 14 TL   ET 1 0 0 RG"),
    (b"q 7 Tr BT (a) Tj ET /Im0 Do Q BT (b) Tj ET", b"q 7 Tr BT  ET 0 0 m h W n /Im0 Do Q BT  ET"),
    (b"q 7 Tr Q BT (a) Tj ET 0 0 1 rg", b"q 7 Tr Q BT  ET 0 0 1 rg"),  # the mode came back with Q
    (b"1 w 0 J BT (Tj) Tj ET % (x) Tj\n0 0 m 9 9 l S", b"1 w 0 J BT  ET % (x) Tj\n0 0 m 9 9 l S"),
    (b"BT/F9 9 Tf(a)Tj(b)Tj ET", b"BT/F9 9 Tf ET"),
))
def test_text_goes_from_a_drawing_and_the_rest_stays(src, want):
    """Only the operators that show text go, with their operands: what sets the line state stays byte for byte
    (MuPDF, writing a drawing out anew, left out a line width set to the default)."""
    assert an._without_shows(src) == want


def _type3(text: bytes, proc: bytes, box: str = "0 0 1 1"):
    """A page with a Type3 font whose measures are odd as some old TeX fonts have them: an em of one unit, its
    /FontBBox ``box`` (MuPDF's redaction reads a glyph's place from it), glyphs drawn tens of units tall."""
    doc = pymupdf.open()
    cp, font = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(cp, "<<>>")
    doc.update_stream(cp, proc)
    doc.update_object(font, f"<</Type/Font/Subtype/Type3/FontBBox[{box}]/FontMatrix[1 0 0 1 0 0]/CharProcs<</a {cp} 0 R"
                            ">>/Encoding<</Type/Encoding/Differences[97/a]>>/FirstChar 97/LastChar 97/Widths[50]>>")
    return pymupdf.open("pdf", _page_of(text + b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj ET",
                                        f"/Font<</T3 {font} 0 R/F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>",
                                        doc).tobytes())


@pytest.mark.parametrize("mark, left", (
    ((98, 178, 135, 190), "REDACT_LEFT"),  # over what the glyphs draw: MuPDF misses them, and says nothing
    ((98, 178, 135, 194), ""),  # down to the line they stand on too: MuPDF takes them
))
def test_redaction_stops_at_glyphs_their_font_puts_elsewhere(mark, left):
    """Glyphs 12 points tall whose font says they are 0.2 tall, at the line they stand on: a mark over what they
    draw left them in the file, under the black box, without a word."""
    doc = _type3(b"BT /T3 0.2 Tf 100 600 Td (aaa) Tj ET ", b"50 0 0 0 40 60 d1 0 0 40 60 re f")
    an.mark_redaction(doc[0], mark)
    if left:
        with pytest.raises(ValueError, match=left):
            an.apply_redactions(doc)
    else:
        assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == ["KEEPME"]


UPRIGHT = ("<</Type/Font/Subtype/Type0/BaseFont/HeiseiMin-W3/Encoding/Identity-V/DescendantFonts[<</Type/Font"
           "/Subtype/CIDFontType0/BaseFont/HeiseiMin-W3/CIDSystemInfo<</Registry(Adobe)/Ordering(Japan1)/Supplement 2"
           ">>/FontDescriptor<</Type/FontDescriptor/FontName/HeiseiMin-W3/Flags 4/FontBBox[-123 -257 1124 910]"
           "/ItalicAngle 0/Ascent 910/Descent -257/CapHeight 709/StemV 69>>>>]>>")  # MuPDF brings a font for it


@pytest.mark.parametrize("mark, refused", (
    ((290, 113, 310, 140), True),  # い and う, with room: MuPDF took お, away from the mark
    ((290, 117, 310, 136), True),  # い and う, tight
    ((60, 280, 200, 300), False),  # text across the page, away from the column
))
def test_redaction_stops_at_upright_text(mark, refused):
    """MuPDF's redaction reads a glyph of text written downwards from where the pen stands, a glyph's height above
    where it draws it (あいうえお here, a glyph every 12 points from 102.6 down). A mark over い and う with room
    took お too, away from the mark, and the check, which reads glyphs as MuPDF does, let it go. Upright text
    cannot be redacted: nothing is, and the program says so. Text across the page next to it can."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    c = doc.get_new_xref()
    doc.update_object(c, "<<>>")
    doc.update_stream(c, b"BT /FV 12 Tf 1 0 0 1 300 700 Tm <034b034d034f03510353> Tj ET"
                         b" BT /F9 12 Tf 72 500 Td (SECRET1) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<</Font<</FV {UPRIGHT}/F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica"
                                             f">>>>>>")
    doc = pymupdf.open("pdf", doc.tobytes())
    assert doc[0].get_text().split() == list("あいうえお") + ["SECRET1"]
    an.mark_redaction(doc[0], mark)
    if refused:
        with pytest.raises(ValueError, match="REDACT_MOVED"):
            an.apply_redactions(doc)
    else:
        assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == list("あいうえお")


def test_redaction_reads_the_place_of_a_glyph_as_mupdf_does():
    """A Type3 font says its glyphs reach 38 ems up (/FontBBox), and MuPDF takes the line under the marked one, whose
    glyphs reach into the mark that way. The check after the rewrite read them as an em tall, as PyMuPDF does, and
    stopped the redaction as if the rewrite had lost them."""
    doc = _type3(b"BT /T3 0.24 Tf 100 600 Td (aaa) Tj 0 -10 Td (aaa) Tj ET ", b"50 0 0 0 30 30 d1 0 0 30 30 re f",
                 "0 -9 42 38")
    an.mark_redaction(doc[0], (95, 186, 140, 195))  # line 1, and line 2's box from its font
    assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == ["KEEPME"]


def test_redaction_leaves_a_small_glyph_that_reaches_into_the_mark():
    """A glyph of the next line that reaches a little way into a mark, as a Thai tone mark over its letter does,
    stays: its place, from its font, is outside the mark, and so is most of it on screen."""
    doc = _type3(b"BT /T3 0.2 Tf 100 600 Td (a) Tj ET ", b"50 0 0 0 40 5 d1 0 0 40 5 re f")  # 1 point tall
    an.mark_redaction(doc[0], (98, 160, 135, 191.6))  # over its top 0.6 points
    assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == ["a", "KEEPME"]


@pytest.mark.parametrize("rot", (0, 90, 180, 270))
@pytest.mark.parametrize("box", tuple(ODD_BOXES))
def test_redaction_fill_covers_the_mark_on_any_page(box, rot):
    """PyMuPDF paints a mark's fill through page geometry of its own: on a turned page whose boxes start away from
    0,0, and on a page with a /UserUnit, the black box landed elsewhere, over words that stay, and left the place
    of the words it took white."""
    doc, page = _odd_page(box, rot)
    hit = pymupdf.Rect(pt.search_rects(doc, "MARK")[0]["r"]) + (-4, -4, 4, 4)
    an.mark_redaction(page, hit)
    assert an.apply_redactions(doc) == 1
    ink = _ink(doc[0])
    assert ink is not None and all(abs(a - b) <= 1.5 for a, b in zip(ink, hit)), (ink, hit)


def test_redaction_fills_the_quads_of_a_mark_made_elsewhere_in_its_colour():
    """A mark made in another program fills what it took, its /QuadPoints, in its own colour (/IC)."""
    doc = _page_of(SECRET + KEEP)
    a = doc[0].add_redact_annot((300, 300, 320, 320))
    doc.xref_set_key(a.xref, "QuadPoints", "[70 715 130 715 70 694 130 694]")  # over SECRET1, in PDF space
    doc.xref_set_key(a.xref, "IC", "[0 1 0]")
    doc = pymupdf.open("pdf", doc.tobytes())
    assert an.apply_redactions(doc) == 1
    assert _ink(doc[0], "green") == pymupdf.Rect(70, 77, 130, 98) and _ink(doc[0], clip=(290, 290, 330, 330)) is None


@pytest.mark.parametrize("case", ("blend mode", "picture"))
def test_redaction_keeps_what_a_drawing_takes_from_the_page(case):
    """A drawing (form XObject) without resources of its own takes those of the page that draws it. MuPDF's copy of
    it looked them up in its copy of the page's resources, which held only what the page had used up to there: a
    highlight lost its blend mode and covered the words under it, a picture went."""
    doc = pymupdf.open()
    im, fm = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(im, "<</Type/XObject/Subtype/Image/Width 1/Height 1/ColorSpace/DeviceRGB/BitsPerComponent 8>>")
    doc.update_stream(im, b"\x20\x60\xe0")
    doc.update_object(fm, "<</Type/XObject/Subtype/Form/BBox[0 0 612 792]>>")
    doc.update_stream(fm, b"/GM gs 1 0.75 0.63 rg 60 480 200 40 re f" if case == "blend mode"
                      else b"q 40 0 0 40 300 480 cm /Im0 Do Q")
    _page_of(SECRET + KEEP + b" /Fm0 Do", f"/ExtGState<</GM<</BM/Multiply>>>>/XObject<</Fm0 {fm} 0 R/Im0 {im} 0 R>>",
             doc)
    doc = pymupdf.open("pdf", doc.tobytes())
    before = doc[0].get_pixmap(dpi=72, clip=(40, 250, 400, 330)).samples
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert _alike(doc[0].get_pixmap(dpi=72, clip=(40, 250, 400, 330)).samples, before)
    assert doc.xref_get_key(fm, "Resources") == ("null", "null")  # the original as it was, for all else drawing it


def _alike(a: bytes, b: bytes) -> bool:
    """Two renderings that differ at most in how an edge is smoothed."""
    return len(a) == len(b) and max(abs(x - y) for x, y in zip(a, b)) <= 8


def _away(page, mark=None) -> list:
    """The glyphs a page draws away from ``mark``: glyph, place and how drawn (filled, invisible...)."""
    return sorted((g[0], g[1], g[2], g[4]) for g in an._placed(page)
                  if mark is None or not pymupdf.Rect(g[3]).intersects(mark))


def _same(was: list, now: list) -> bool:
    """The same glyphs at the same places, drawn the same way, give or take a hair (a nudge of _steady)."""
    return len(was) == len(now) and all(a[0] == b[0] and abs(a[1] - b[1]) < 0.02 and abs(a[2] - b[2]) < 0.02
                                        and a[3] == b[3] for a, b in zip(was, now))


STEADY_CASES = {
    "0 0 Td": (b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj 0 0 Td (KEEPME) Tj ET", b""),  # overprint, as fake bold has it
    "the same Tm": (b"BT /F9 12 Tf 1 0 0 1 72 500 Tm (KEEPME) Tj 1 0 0 1 72 500 Tm (KEEPME) Tj ET", b""),
    "there and back": (b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj 10 0 Td -10 0 Td (KEEPME) Tj ET", b""),
    "T* without spacing": (b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj T* (KEEPME) Tj ET", b""),
    "'": (b"BT /F9 12 Tf 14 TL 72 500 Td (KEEP1) Tj (KEEP2) ' (KEEP3)' ET", b""),
    '"': (b'BT /F9 12 Tf 14 TL 72 500 Td (KEEP1) Tj 3 1 (KEEP 2) " ET', b""),
    "0 Tc in a drawing": (b"BT /F9 12 Tf 2 Tc 72 600 Td (KEEP1) Tj ET /Fm0 Do",
                          b"BT 0 Tc 0 Tw /F9 12 Tf 72 400 Td (KEEP 2) Tj ET"),  # a footer as Acrobat writes one
    "0 0 Td in a drawing": (b"/Fm0 Do", b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj 0 0 Td (KEEPME) Tj ET"),
    "0 Tr in a drawing": (b"BT /F9 12 Tf 3 Tr 72 600 Td (OCRTEXT) Tj ET 0 Tr /Fm0 Do",  # a footer on an OCR page
                          b"BT 0 Tr /F9 12 Tf 72 400 Td (FOOTER) Tj ET"),
    "placed by cm": (b"q 1 0 0 1 72 500 cm BT /F9 12 Tf (KEEPME) Tj ET Q", b""),  # after SECRET1 ended its text
    "after a TJ ending in a number": ((b"BT /F9 12 Tf 72 520 Td [(KEEP1) -250] TJ ET "
                                       b"q 1 0 0 1 72 500 cm BT /F9 12 Tf (KEEP2) Tj ET Q"), b""),
    "0 0 Td where cm placed it": (b"q 1 0 0 1 72 500 cm BT /F9 12 Tf (KEEPME) Tj 0 0 Td (KEEPME) Tj ET Q", b""),
    "a hair back": (b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj -0.0000001 0 Td (KEEPME) Tj ET", b""),  # 72 to a float
}


def _steady_doc(case: str, pages: int = 1):
    page, form = STEADY_CASES[case]
    doc = pymupdf.open()
    fm = doc.get_new_xref()
    doc.update_object(fm, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
    doc.update_stream(fm, form or b" ")
    for _ in range(pages):
        _page_of(SECRET + page, f"/XObject<</Fm0 {fm} 0 R>>", doc)
    return pymupdf.open("pdf", doc.tobytes()), fm


@pytest.mark.parametrize("case", tuple(STEADY_CASES))
def test_redaction_leaves_text_away_from_the_mark_in_place(case):
    """MuPDF 1.28.2 writes the page out anew to redact it and moved text away from the mark: it left out a move
    back to where text was placed before (overprinting, also where BT placed it), moved ' and " lines by a spacing
    of its own (off the page), gave a copy of a drawing the character spacing and render mode of where it is drawn
    where the drawing set them to the default (a footer turned invisible on a page with OCR text), and moved the
    first text of a text object placed by cm alone by the room left at the end of the one before (SECRET1 taken
    out, a number ending a TJ)."""
    doc, _ = _steady_doc(case)
    mark = pymupdf.Rect(60, 75, 200, 100)
    before = _away(doc[0], mark)
    an.mark_redaction(doc[0], mark)
    assert an.apply_redactions(doc) == 1
    assert _same(before, _away(doc[0], mark)) and "SECRET" not in doc[0].get_text()


@pytest.mark.parametrize("around", ("page", "drawing"))
def test_redaction_keeps_the_line_width_a_drawing_sets(around):
    """A drawing that strokes with 1 w after the page, or the drawing it is in, set 5 w: MuPDF's copy left out its
    1 w (the default, as it thinks) and stroked five times as wide."""
    doc = pymupdf.open()
    fm, outer = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(fm, "<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<>>>>")
    doc.update_stream(fm, b"1 w 72 200 m 300 200 l S")
    doc.update_object(outer, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<</XObject<</Fm0 {fm} 0 R>>>>>>")
    doc.update_stream(outer, b"5 w 72 300 m 300 300 l S /Fm0 Do")
    if around == "page":
        _page_of(SECRET + b" 5 w 72 300 m 300 300 l S /Fm0 Do", f"/XObject<</Fm0 {fm} 0 R>>", doc)
    else:
        _page_of(SECRET + b" /Fm1 Do", f"/XObject<</Fm1 {outer} 0 R>>", doc)
    doc = pymupdf.open("pdf", doc.tobytes())
    before = doc[0].get_pixmap(dpi=72, clip=(60, 470, 320, 610)).samples
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert _alike(doc[0].get_pixmap(dpi=72, clip=(60, 470, 320, 610)).samples, before)


SIZE_CASES = {  # a line, and what on it goes (searched for, the room before it too with a leading space)
    "an exponent": (b"BT /F9 12 Tf 72 700 Td (E = mc) Tj 6 Ts /F9 8 Tf (2) Tj 0 Ts /F9 12 Tf ( is energy) Tj ET",
                    "2"),
    "a bullet": (b"BT /F9 9.96 Tf 72 700 Td (-) Tj ( ) Tj /F9 11.04 Tf (SECRET1 kept) Tj ET", " SECRET1"),
    "a TJ ending in a number": ((b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET "
                                 b"BT /F9 12 Tf 72 500 Td [(kept) -250] TJ /F9 6 Tf <78> Tj ET"), "SECRET1"),
    "a scaling": (b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj 50 Tz ( kept) Tj T* (next) Tj ET", "SECRET1"),
    "twice on a line": ((b"BT /F9 12 Tf 14 TL 72 700 Td (SECRET1) Tj /F9 8 Tf (2) Tj /F9 12 Tf ( kept) Tj "
                         b"/F9 6 Tf (\\(x\\)) Tj T* (next) Tj 0 -14 Td (third) Tj ET"), "SECRET1"),
    "a size set in q Q": (b"/F9 12 Tf q /F9 8 Tf Q BT 72 700 Td (SECRET1) Tj /F9 8 Tf ( kept) Tj ET", "SECRET1"),
    # a line break in a string reads as one LF; white space in a hex string as nothing, a stray byte there as 0
    "line breaks and hex": ((b"BT /F9 12 Tf 0.5 Tc 72 700 Td (SECRET1) Tj (a\r\nb\rc) Tj <2D 2 D41 x> Tj "
                             b"/F9 8 Tf (kept) Tj ET"), "SECRET1"),
    "a comment in a TJ": ((b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET BT /F9 12 Tf 72 500 Td [(Total:) %(note)\n ( 12)] TJ "
                           b"/F9 8 Tf (kept) Tj ET"), "SECRET1"),  # MuPDF reads past what the comment holds
}


@pytest.mark.parametrize("case", tuple(SIZE_CASES))
def test_redaction_keeps_a_line_in_place_where_the_size_changes(case):
    """MuPDF 1.28.2 kept the room of what it took out (and a number ending a TJ) in units of the font size in force
    then, and wrote it out after a Tf or Tz that changed size or scaling: the rest of the line moved (after an
    exponent, a bullet in a smaller size) and the redaction stopped."""
    content, word = SIZE_CASES[case]
    doc = pymupdf.open("pdf", _page_of(content).tobytes())
    r = doc[0].search_for(word.strip())[0]
    mark = pymupdf.Rect(r.x0 - 2 * word.startswith(" "), r.y0, r.x1, r.y1)
    before = _away(doc[0], mark)
    doc[0].add_redact_annot(mark)
    assert an.apply_redactions(doc) == 1
    assert _same(before, _away(doc[0], mark)) and word.strip() not in doc[0].get_text()


def test_redaction_checks_the_place_steady_gives_a_line(monkeypatch):
    """The check after the redaction reads the page as it stood before _steady wrote it: a place it sets wrongly
    (here: from widths a point too wide) stops the redaction rather than move the rest of the line."""
    real = an._run
    monkeypatch.setattr(an, "_run", lambda *args: (real(*args)[0] + 1, real(*args)[1]))
    doc = pymupdf.open("pdf", _page_of(SIZE_CASES["an exponent"][0]).tobytes())
    doc[0].add_redact_annot(doc[0].search_for("2")[0])
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


@pytest.mark.parametrize("font, codes", (("helv", "a b\\(c\\)\\101\\\n"), ("china-s", "测 试")))
def test_text_runs_measure_as_mupdf_places_them(font, codes):
    """_run tells how far a string moves the text matrix as MuPDF works it out (one- and two-byte codes, escapes,
    char and word spacing, horizontal scaling, numbers in a TJ): the glyph after it lands where MuPDF draws it."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "测" if font == "china-s" else "a", fontname=font)
    name = doc.get_page_fonts(0)[0][4]
    raw = codes.encode("utf-16-be") if font == "china-s" else codes.encode()
    token = b"<" + raw.hex().encode() + b">" if font == "china-s" else b"(" + raw + b")"
    page.clean_contents()
    xref = page.get_contents()[0]
    doc.update_stream(xref, b"BT /" + name.encode() + b" 10 Tf 2 Tc 3 Tw 80 Tz 1 0 0 1 72 700 Tm [" + token
                      + b" -500 " + token + b"] TJ <" + b"00" * (font == "china-s") + b"41> Tj ET")  # then an A
    glyphs = an._placed(page)
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    res = m.pdf_dict_get_inheritable(m.pdf_load_object(pdf, page.xref), m.PdfObj(m.PDF_ENUM_NAME_Resources))
    stack = m.pdf_resource_stack()
    stack.resources, stack.next = res.m_internal, None
    fobj = m.pdf_dict_gets(m.pdf_dict_gets(res, "Font"), name)
    f = m.ll_pdf_load_font(pdf.m_internal, stack, fobj.m_internal)
    try:
        items = an._items(b"[" + token + b" -500 " + token + b"]")
        assert items[0] == raw.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\101", b"A").replace(
            b"\\\n", b"") and items[1] == -500
        x = sum(an._run(f, i, 10, 2, 3, 0.8)[0] if isinstance(i, bytes) else -i * 10 * 0.001 * 0.8 for i in items)
    finally:
        m.ll_pdf_drop_font(f)
    assert abs(72 + x - glyphs[-1][1]) < 0.001


def test_redaction_stops_when_the_rewrite_moves_text(monkeypatch):
    """Should text away from the marks still move in MuPDF's rewrite (here: without our help against it), the
    redaction stops with a message and the page stays as it was."""
    monkeypatch.setattr(an, "_steady", lambda data, *args, **kw: data)
    doc, _ = _steady_doc("0 0 Td")
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


@pytest.mark.parametrize("paint", (b"7 Tr", b"/Pattern cs /P0 scn"))
def test_redaction_stops_when_the_rewrite_moves_text_that_clips(monkeypatch, paint):
    """Text that clips (render mode 7), or that a pattern fills (MuPDF clips with it then), is checked as other
    text is: the check saw filled, stroked and unseen text only. Here the rewrite (without our help against it)
    moves the second KEEPME onto the end of the first."""
    monkeypatch.setattr(an, "_steady", lambda data, *args, **kw: data)
    doc = pymupdf.open()
    content = SECRET + b"q BT /F9 12 Tf " + paint + b" 72 500 Td (KEEPME) Tj 0 0 Td (KEEPME) Tj ET Q"
    doc = pymupdf.open("pdf", _page_of(content, f"/Pattern<</P0 {_tiling(doc, b'0 0 1 rg 0 0 612 792 re f')} 0 R>>",
                                       doc).tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


def test_redaction_stops_when_mupdf_drops_text_beside_the_mark():
    """Text shown outside a text object (no BT ... ET), as some writers have it: MuPDF's redaction drops all of it,
    also the $0 five points left of the mark. The check let a glyph go within its height of a mark."""
    doc = pymupdf.open("pdf", _page_of(b"/F9 1 Tf 9 0 0 9 36.6 748.3 Tm [($0)] TJ 9 0 0 9 51.8 748.3 Tm [(00)] TJ")
                       .tobytes())
    an.mark_redaction(doc[0], (51.8, 35.5, 61.8, 47.5))  # over 00
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


def test_redaction_stops_when_mupdf_drops_text_in_a_clip_without_area():
    """MuPDF's redaction drops all that is drawn inside a clip without area (0 0 0 0 re W n): such text never
    shows, but it goes from the file, so nothing is redacted, as with other text MuPDF would lose."""
    doc = pymupdf.open("pdf", _page_of(SECRET + b"q 0 0 0 0 re W n BT /F9 12 Tf 72 500 Td (CLIPPED) Tj ET Q").tobytes())
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    with pytest.raises(ValueError, match="REDACT_MOVED"):
        an.apply_redactions(doc)


@pytest.mark.parametrize("tm, mark", ((b"12 0 6 12 100 500", (99.5, 290, 99.9, 293)),  # leaning: its foot
                                      (b"10.392 6 -6 10.392 100 500", (99, 278.8, 101.5, 279.5))))  # turned 30 degrees
def test_redaction_reads_leaning_and_turned_glyphs_as_mupdf_does(tm, mark):
    """MuPDF takes a glyph whose box, through the glyph's matrix, touches a mark: here A, under a mark beside its
    upright box. The check reads the same box, rather than stop the redaction as if A were lost."""
    doc = pymupdf.open("pdf", _page_of(b"BT /F9 1 Tf " + tm + b" Tm (AB) Tj ET BT /F9 12 Tf 72 300 Td (KEEPME) Tj ET")
                       .tobytes())
    doc[0].add_redact_annot(mark)  # smaller than mark_redaction takes
    assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == ["B", "KEEPME"]


def test_redaction_takes_the_letters_of_a_glyph_together():
    """A glyph that stands for more than one letter (here A for fi, by its /ToUnicode, as ligatures and Arabic
    have it): MuPDF reads the second letter as a glyph of no width where the glyph starts. Under a mark on the
    right half of A, both go with it; the check saw the i lost away from the mark."""
    doc = pymupdf.open()
    cmap = doc.get_new_xref()
    doc.update_object(cmap, "<<>>")
    doc.update_stream(cmap, b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /L def "
                            b"1 begincodespacerange <00> <FF> endcodespacerange 2 beginbfchar <41> <00660069> "
                            b"<58> <0058> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end")
    font = f"/F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode {cmap} 0 R>>"
    doc = pymupdf.open("pdf", _page_of(b"BT /F1 24 Tf 100 600 Td (XA) Tj ET BT /F9 12 Tf 72 300 Td (KEEPME) Tj ET",
                                       f"/Font<<{font}/F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>",
                                       doc).tobytes())
    assert doc[0].get_text().split() == ["Xfi", "KEEPME"]
    an.mark_redaction(doc[0], (125, 170, 132, 195))
    assert an.apply_redactions(doc) == 1 and doc[0].get_text().split() == ["X", "KEEPME"]


def test_glyph_boxes_are_the_ones_mupdf_takes_glyphs_by():
    """The box _placed gives a glyph is the one MuPDF's redaction tests it against: a tiny mark just inside each
    edge of it takes the glyph, one just outside leaves it (in leaning text, where each corner counts)."""
    data = _page_of(b"BT /F9 1 Tf 12 0 6 12 100 500 Tm (A) Tj ET").tobytes()
    x0, y0, x1, y1 = an._placed(pymupdf.open("pdf", data)[0])[0][3]
    mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
    for inside, (x, y) in ((True, (x0 + 0.05, mid_y)), (True, (x1 - 0.05, mid_y)), (True, (mid_x, y0 + 0.05)),
                           (True, (mid_x, y1 - 0.05)), (False, (x0 - 0.15, mid_y)), (False, (x1 + 0.15, mid_y)),
                           (False, (mid_x, y0 - 0.15)), (False, (mid_x, y1 + 0.15))):
        doc = pymupdf.open("pdf", data)
        doc[0].add_redact_annot((x - 0.05, y - 0.05, x + 0.05, y + 0.05))
        an._redact(doc, doc[0])
        assert (not an._placed(doc[0])) == inside, (inside, x, y)


def test_redaction_takes_an_original_another_page_names_but_does_not_draw():
    """Page 2 names the drawing page 1 draws, but draws text of its own only: the name kept the original, words and
    all, in the file. It leaves page 2's resources; page 2's content stays as it was, byte for byte."""
    doc = pymupdf.open()
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<{HELV}>>>>")
    doc.update_stream(fx, b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET")
    for content in (b"/Fm0 Do" + KEEP, STEADY_CASES["0 0 Td"][0]):  # page 2 overprints, which a rewrite moved
        _page_of(content, f"/XObject<</Fm0 {fx} 0 R>>", doc)
    doc = pymupdf.open("pdf", doc.tobytes())
    data, glyphs = [doc.xref_stream(x) for x in doc[1].get_contents()], _away(doc[1])
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert [doc.xref_stream(x) for x in doc[1].get_contents()] == data and _away(doc[1]) == glyphs
    assert b"SECRET" not in _raw(doc) and b"KEEPME" in _raw(doc)


def test_redaction_gives_a_drawing_other_pages_draw_back_as_it_was():
    """The drawing is changed in place for MuPDF to copy; the original, which page 2 draws, gets its own back."""
    doc, fm = _steady_doc("0 0 Td in a drawing", pages=2)
    data = doc.xref_stream(fm)
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert doc.xref_stream(fm) == data and fm in {x[0] for x in doc.get_page_xobjects(1)}


@pytest.mark.parametrize("case", ("0 0 Td", "there and back", "'", '"'))
def test_steady_draws_what_it_was_given(case):
    """What _steady writes draws the same glyphs at the same places, read as it stands: a nudge is a hair, and is
    taken back before the next move."""
    data = STEADY_CASES[case][0]
    new = an._steady(data)
    assert new != data and _same(*(_away(_page_of(d)[0]) for d in (data, new)))


@pytest.mark.parametrize("case", tuple(SIZE_CASES))
def test_steady_sets_the_place_a_line_has_got_to(case):
    """Before a Tf or Tz that changes size or scaling after text on a line, _steady sets the place the line has
    got to, from the widths of the glyphs before it: the page draws the same glyphs at the same places."""
    doc = _page_of(SIZE_CASES[case][0])
    before = _away(doc[0])
    an._steady_page(doc, doc[0])
    assert b" Tm " in b"".join(doc.xref_stream(x) for x in doc[0].get_contents())
    assert _same(before, _away(doc[0]))


def test_steady_turns_quotes_into_moves_and_shows():
    assert an._steady(b"BT 14 TL (a)' 1 2 (b)\" T*(c)' ET") == \
        b"BT 1 0 0 1 0 0 Tm  14 TL  T* (a)Tj  1 Tw 2 Tc  T* (b)Tj T* T* (c)Tj ET"
    assert an._steady(b"BT 0 Tc 0 Tw (a) Tj ET", None, copied=True) == \
        b"BT 1 0 0 1 0 0 Tm  0.000001 Tc 0.000001 Tw (a) Tj ET"


@pytest.mark.parametrize("cs", ("/G", "/CS0", "ref"))  # CS0: of the page's resources, three bytes a pixel
def test_redaction_leaves_an_inline_picture_whose_bytes_read_ei_as_it_was(cs):
    """MuPDF reads an inline picture by its size, so its bytes can read ' EI BT ': the scanner took the first EI
    between white space for the end, read the rest of the pixels as content, and _steady wrote into them. A
    dictionary that points to an object (/CS 5 0 R) was read without the file, and so was its picture."""
    doc = pymupdf.open()
    if cs == "ref":
        cs = f"{doc.get_new_xref()} 0 R"
        doc.update_object(int(cs.split()[0]), "/DeviceGray")
    pixels = bytes(range(0, 200, 20)) + b" EI BT " + bytes(range(255, 15, -16))
    w = 32 // 3 + 1 if cs == "/CS0" else 32  # 33 bytes from 11 pixels: one more byte of the run
    pixels = pixels + b"\x80" * (3 * w - len(pixels)) if cs == "/CS0" else pixels
    picture = b"q 320 0 0 40 72 300 cm BI /W %d /H 1 /BPC 8 /CS %s ID " % (w, cs.encode()) + pixels + b" EI Q "
    doc = pymupdf.open("pdf", _page_of(SECRET + picture + KEEP, "/ColorSpace<</CS0/DeviceRGB>>", doc).tobytes())
    before = doc[0].get_pixmap(dpi=72, clip=(72, 452, 392, 492)).samples
    an.mark_redaction(doc[0], (60, 75, 200, 100))
    assert an.apply_redactions(doc) == 1
    assert doc[0].get_pixmap(dpi=72, clip=(72, 452, 392, 492)).samples == before


def test_redaction_reads_on_after_an_inline_picture_whose_ei_follows_its_data():
    """MuPDF takes the EI right after the data of a picture, white space before it or not: the scanner read on to
    the end of the stream, and the overprint after the picture moved (the redaction stopped)."""
    mask = b"q 16 0 0 17 300 300 cm BI /W 16 /H 17 /BPC 1 /IM true ID " + b"\xff" * 34 + b"EI Q "
    doc = pymupdf.open("pdf", _page_of(SECRET + mask + STEADY_CASES["0 0 Td"][0]).tobytes())
    mark = pymupdf.Rect(60, 75, 200, 100)
    before = _away(doc[0], mark)
    an.mark_redaction(doc[0], mark)
    assert an.apply_redactions(doc) == 1
    assert _same(before, _away(doc[0], mark))


def test_redaction_keeps_a_picture_another_page_draws_after_an_inline_picture():
    """Page 2 shares its resources and a picture with page 1, and draws the picture after an inline picture whose
    dictionary points to an object and whose bytes read ' EI (': the scanner ended the inline picture there, read
    the rest of the stream as a string, did not see page 2 draw the picture, and took it from its resources when
    the redaction on page 1 gave page 1 a new one."""
    doc = pymupdf.open()
    cs, img, res = doc.get_new_xref(), doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(cs, "/DeviceGray")
    doc.update_object(img, "<</Type/XObject/Subtype/Image/Width 2/Height 2/BitsPerComponent 8/ColorSpace/DeviceRGB>>")
    doc.update_stream(img, bytes([255, 0, 0] * 4))
    doc.update_object(res, f"<<{HELV}/XObject<</Im0 {img} 0 R>>>>")
    pixels = (b"\x10\x30 EI ( " + bytes(range(0, 255, 3)))[:64]
    for content in (b"q 200 0 0 200 100 400 cm /Im0 Do Q " + SECRET,
                    b"q BI /W 64 /H 1 /BPC 8 /CS %d 0 R ID " % cs + pixels + b" EI Q q 200 0 0 200 100 400 cm /Im0 Do Q"):
        page = doc.new_page(width=612, height=792)
        c = doc.get_new_xref()
        doc.update_object(c, "<<>>")
        doc.update_stream(c, content)
        doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
        doc.xref_set_key(page.xref, "Resources", f"{res} 0 R")
    doc = pymupdf.open("pdf", doc.tobytes())
    before = doc[1].get_pixmap(dpi=36, clip=(110, 200, 290, 380)).samples
    an.mark_redaction(doc[0], (150, 250, 250, 300))  # over part of the picture on page 1
    assert an.apply_redactions(doc) == 1
    assert pymupdf.open("pdf", doc.tobytes())[1].get_pixmap(dpi=36, clip=(110, 200, 290, 380)).samples == before


@pytest.mark.parametrize("x", (b"72", b"600000"))  # far out, as a drawing with a tiny cm has it
def test_steady_takes_a_page_of_moves_in_its_stride(x):
    """A page set as one text object with a move per word: _steady looked for each place among all those before
    (6 s for 4000 words, and 45 s for 8000 far out, where floats tell places apart less finely), now among those
    next to it."""
    words = b" ".join(b"-413 -12 Td (w) Tj" if i % 60 == 0 else b"7 0 Td (w) Tj" for i in range(8000))
    t0 = time.perf_counter()
    an._steady(b"BT /F9 10 Tf " + x + b" 760 Td " + words + b" ET")
    assert time.perf_counter() - t0 < 2


@pytest.mark.parametrize("x, step", ((b"72", b"0"), (b"600000", b"0"), (b"-600000.5", b"0"), (b"3000000", b"0"),
                                     (b"1048575.5", b"1"), (b"1048576.5", b"-1")))  # 1 apart, across 2**20
def test_steady_nudges_an_overprint_near_and_far(x, step):
    """Where a move ends counts as where text was placed from before as floats tell, finer near the origin than far
    out: an overprint gets its nudge everywhere, also where the two places lie in cells of different sizes."""
    data = b"BT /F9 12 Tf " + x + b" 500 Td (A) Tj " + step + b" 0 Td (A) Tj ET"
    assert an._steady(data).count(b" Td ") == 3


def test_scanner_steps_over_a_comment_in_an_array():
    """A comment in an array or dictionary is white space to MuPDF, whatever it holds: a ( there began a string
    for the scanner, which then read on past the array to the end of the stream."""
    data = b"BT [(A) % ( a note\n (B)] TJ (C) Tj ET"
    assert [(op, [data[s:e] for s, e in args]) for op, args, *_ in an._operators(data, (b"TJ", b"Tj"))] == \
        [(b"TJ", [b"[(A) % ( a note\n (B)]"]), (b"Tj", [b"(C)"])]


def test_marked_content_scanner_reads_tokens_as_mupdf_does():
    """Only real BDC and Do operators count, with their operands: not in strings, comments, names, inline picture
    data or longer words, and not where a long run of plain operations (read in one match) ends. An ID without BI
    is a word MuPDF skips, no picture."""
    lines = b"".join(b"%d %d m %d %d l S\n" % (i, i, i + 3, i + 2) for i in range(10000))
    data = (b"ID /Fm2 Do (BDC) Tj % BDC Do\n/BDC /Do BDCx xDo BI /W 1 /H 1 ID BDC EI " + lines +
            b"/P <</MCID 0 /A [(x) <41>] >>BDC /Fm0 Do/Fm1 Do\x00/Span/Q BDC true false null Do")
    found = [(op, [data[s:e] for s, e in args]) for op, args, *_ in an._operators(data)]
    assert found == [(b"Do", [b"/Fm2"]), (b"BDC", [b"/P", b"<</MCID 0 /A [(x) <41>] >>"]), (b"Do", [b"/Fm0"]),
                     (b"Do", [b"/Fm1"]), (b"BDC", [b"/Span", b"/Q"]), (b"Do", [b"true", b"false", b"null"])]


def test_doc_annots_lists_pages():
    doc = make_doc(2)
    an.add_note(doc[1], (100, 100), "druga")
    items = an.doc_annots(doc)
    assert len(items) == 1 and items[0]["p"] == 1 and items[0]["txt"] == "druga"


# --- fixes from the October audit -----------------------------------------------------


def test_hexcolor_reads_gray_and_cmyk():
    from core.geom import hexcolor
    assert hexcolor([0.5]) == "#808080"
    assert hexcolor([0, 1, 1, 0]) == "#ff0000"
    assert hexcolor([0, 0, 0, 1]) == "#000000"
    assert hexcolor([1, 0, 0]) == "#ff0000"


def test_redaction_by_search_takes_words_written_with_ligatures():
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(font):
        pytest.skip("DejaVuSans not installed")
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 100), "Contact: Raﬁq Suﬃx office", fontfile=font, fontname="dj", fontsize=14)
    assert an.mark_redaction_text(doc, "Rafiq") == 1 and an.mark_redaction_text(doc, "Suffix") == 1
    an.apply_redactions(doc)
    text = doc[0].get_text()
    assert "office" in text and "Ra" not in text and "Su" not in text


def test_redaction_by_search_takes_the_words_out_of_bookmarks(tmp_path):
    doc = make_doc(2)
    doc.set_toc([[1, "Notes on marker-1", 1], [2, "more MARKER-1 here", 2], [1, "Other", 2]])
    assert an.mark_redaction_text(doc, "marker-1")
    an.apply_redactions(doc)
    assert doc.get_toc() == [[1, "Notes on █", 1], [2, "more █ here", 2], [1, "Other", 2]]
    path = str(tmp_path / "r.pdf")
    pt.save_pdf(doc, path)
    saved = pymupdf.open(path)
    assert saved.get_toc() == doc.get_toc()
    assert not any("marker-1" in saved.xref_object(x).lower() for x in range(1, saved.xref_length()))


def test_moving_or_editing_a_comment_keeps_the_drawing_another_program_made():
    doc = make_doc(1)
    page = doc[0]
    stamp = page.add_stamp_annot(pymupdf.Rect(100, 100, 220, 150), stamp=0)
    stamp.set_info(title="Acrobat")
    ap = int(doc.xref_get_key(stamp.xref, "AP/N")[1].split()[0])
    drawing = b"0 1 0 rg 0 0 5 5 re f"
    doc.update_stream(ap, drawing)
    rect = pymupdf.Rect(page.load_annot(stamp.xref).rect)
    an.move_annot(page, stamp.xref, 10, 0)
    an.edit_annot(page, stamp.xref, author="Marko")
    a = page.load_annot(stamp.xref)
    assert doc.xref_stream(int(doc.xref_get_key(a.xref, "AP/N")[1].split()[0])) == drawing
    assert abs(a.rect.x0 - rect.x0 - 10) < 0.01 and a.info["title"] == "Marko"
    box = page.add_freetext_annot(pymupdf.Rect(100, 300, 300, 340), "Hi", fontname="Cour", fontsize=11)
    doc.xref_set_key(box.xref, "DA", "(/Arial 11 Tf 0 g)")  # a font of another program's AcroForm
    an.move_annot(page, box.xref, 0, 10)
    an.edit_annot(page, box.xref, author="Marko")
    assert doc.xref_get_key(box.xref, "DA")[1] == "/Arial 11 Tf 0 g"


def test_redaction_reads_a_comment_between_a_key_and_its_value():
    """<</ActualText %c\n(...)>>: the comment taken for the value commented out the rest of the page MuPDF writes on
    one line, and the page lost all its text."""
    doc = make_doc(1)
    page = doc[0]
    x = page.get_contents()[0]
    doc.update_stream(x, b"/P <</MCID 0 /ActualText %c\n(Said again)>> BDC\n" + doc.xref_stream(x) + b"\nEMC")
    page.add_redact_annot(page.search_for("marker-1")[0])
    an.apply_redactions(doc)
    text = doc[0].get_text()
    assert "Said again" in text and "lorem ipsum dolor" in text and "marker-1" not in text
