"""Engine tests: every core operation on a real PyMuPDF document.

Rotated pages are checked in display space (what the user sees), because that is where
the v1 engine went wrong. Run: python -m pytest tests
"""
import os

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


@pytest.mark.parametrize("rot", ROTATIONS)
def test_watermark_text_upright_and_centered(rot):
    doc = make_doc(1, rotation=rot)
    page = doc[0]
    pt.watermark_text(doc, "POVERLJIVO Đ", rotate=0, fontsize=40, opacity=0.3)
    hits = [(r, t) for r, t in words_display(page) if t == "POVERLJIVO"]
    assert hits, "watermark text missing"
    r = hits[0][0]
    assert r.width > r.height, "watermark should read horizontally on screen"
    cx = page.rect.width / 2
    assert r.x0 < cx < r.x1 + 40  # the two words straddle the centre
    assert abs((r.y0 + r.y1) / 2 - page.rect.height / 2) < 30


@pytest.mark.parametrize("rot", ROTATIONS)
def test_watermark_angle_is_visual(rot):
    doc = make_doc(1, rotation=rot)
    pt.watermark_text(doc, "DRAFT", rotate=45, fontsize=40)
    angles = line_angles(doc[0])
    assert 45 in angles, angles


def test_watermark_tile_and_unicode():
    doc = make_doc(1)
    pt.watermark_text(doc, "Ђорђе čćđ", tile=True, fontsize=24)
    text = doc[0].get_text()
    assert text.count("Ђорђе") >= 3 and "čćđ" in text


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


def test_merge_extract_split_images(tmp_path):
    src = save_tmp(make_doc(3), tmp_path, "src.pdf")
    out = os.path.join(tmp_path, "m.pdf")
    assert pt.merge([src, src], out) == 6
    doc = make_doc(3)
    pt.extract_pages(doc, [2, 0], os.path.join(tmp_path, "e.pdf"))
    e = pymupdf.open(os.path.join(tmp_path, "e.pdf"))
    assert len(e) == 2 and "page 3" in e[0].get_text()
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
    size = pt.compress_to(doc, small, image_dpi=100, quality=60)
    assert size < os.path.getsize(plain) * 0.7


def test_docx_export(tmp_path):
    pytest.importorskip("pdf2docx")
    src = save_tmp(make_doc(2), tmp_path, "w.pdf")
    out = os.path.join(tmp_path, "w.docx")
    seen = []
    pt.export_docx(src, out, progress=lambda d, t: seen.append((d, t)) or True)
    assert os.path.getsize(out) > 1000 and seen


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


def test_doc_annots_lists_pages():
    doc = make_doc(2)
    an.add_note(doc[1], (100, 100), "druga")
    items = an.doc_annots(doc)
    assert len(items) == 1 and items[0]["p"] == 1 and items[0]["txt"] == "druga"
