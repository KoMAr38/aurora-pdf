"""Engine tests: every core operation on a real PyMuPDF document.

Rotated pages are checked in display space (what the user sees), because that is where
the v1 engine went wrong. Run: python -m pytest tests
"""
import os
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


def test_redaction_takes_a_radio_choice_with_its_button():
    """The group's value names the chosen button. With that button redacted, the other one shows Off and the
    value would still say which was chosen."""
    doc = make_doc(1)
    page = doc[0]
    group, kids, faces = doc.get_new_xref(), (doc.get_new_xref(), doc.get_new_xref()), []
    for draw in ("q 0 g 4 4 8 8 re f Q", ""):
        faces.append(doc.get_new_xref())
        doc.update_object(faces[-1], "<</Type/XObject/Subtype/Form/BBox[0 0 16 16]>>")
        doc.update_stream(faces[-1], draw.encode())
    doc.update_object(group, f"<</FT/Btn/Ff 49152/T(hiv)/V/positive/DV/positive/Kids[{kids[0]} 0 R {kids[1]} 0 R]>>")
    for kid, state, y in zip(kids, ("positive", "negative"), (100, 140)):
        doc.update_object(kid, f"<</Type/Annot/Subtype/Widget/Rect[50 {y} 66 {y + 16}]/Parent {group} 0 R/F 4"
                               f"/P {page.xref} 0 R/AS/{'positive' if state == 'positive' else 'Off'}"
                               f"/AP<</N<</{state} {faces[0]} 0 R/Off {faces[1]} 0 R>>>>>>")
    doc.xref_set_key(page.xref, "Annots", f"[{kids[0]} 0 R {kids[1]} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"<</Fields[{group} 0 R]>>")
    page = doc.reload_page(page)
    an.mark_redaction(page, (45, 595 - 120, 75, 595 - 95))  # the "positive" button only
    assert an.apply_redactions(doc) == 1
    assert [doc.xref_get_key(group, k)[0] for k in ("V", "DV")] == ["null", "null"]
    assert doc.xref_get_key(group, "T") == ("string", "hiv")  # the group and its other button stay


def test_doc_annots_lists_pages():
    doc = make_doc(2)
    an.add_note(doc[1], (100, 100), "druga")
    items = an.doc_annots(doc)
    assert len(items) == 1 and items[0]["p"] == 1 and items[0]["txt"] == "druga"
