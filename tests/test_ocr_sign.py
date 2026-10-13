"""OCR with the bundled language data, and certificate signing/validation."""
import io
import os
import re

import pymupdf
import pytest

from conftest import FONT, make_doc, png_bytes
from core import annots as an
from core import ocr, signing
from core import pdftools as pt


def scanned_doc(rotation: int = 0) -> pymupdf.Document:
    """One page holding only a picture of text, upright on screen."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1240, 1754  # A4 at 150 dpi
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 46)
    for i, t in enumerate(["Ugovor o saradnji broj 42", "Čačak i Šabac su gradovi", "Hello from Aurora"]):
        d.text((120, 200 + i * 95), t, font=f, fill=0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    if rotation in (90, 270):
        page = doc.new_page(width=842, height=595)
    else:
        page = doc.new_page(width=595, height=842)
    page.set_rotation(rotation)
    # place the image so it reads upright on screen
    page.insert_image(page.rect * page.derotation_matrix, stream=buf.getvalue(), rotate=rotation)
    return doc


@pytest.mark.parametrize("rot", (0, 90))
def test_ocr_adds_invisible_text_in_place(rot):
    doc = scanned_doc(rot)
    page = doc[0]
    assert ocr.looks_scanned(doc) and not ocr.page_has_text(page)
    n = ocr.ocr_page(page, "srp_latn+eng", dpi=200)
    assert n >= 10
    text = page.get_text()
    assert "saradnji" in text and "Šabac" in text and "Aurora" in text
    layer = an.text_layer(page)
    first = next(w for w in layer if w[5].startswith("Ugovor"))
    assert first[4] == 0  # upright on screen
    assert abs(first[0] - 120 * 72 / 150) < 6  # where the word is in the picture
    # invisible: rendering is unchanged where the text sits (no extra ink)
    assert not ocr.looks_scanned(doc)


def test_ocr_languages_available():
    langs = ocr.available_languages()
    assert {"srp_latn", "srp", "nor", "eng"} <= set(langs)


def _signed_ok(path, password=None):
    with open(path, "rb") as f:
        data = f.read()
    return signing.validate(data, password)


@pytest.mark.parametrize("rot", (0, 90))
def test_create_id_sign_validate(tmp_path, rot):
    ident = signing.create_id("Marko Đorđević", "tajna123", email="m@example.com", org="Test")
    assert any(i["id"] == ident["id"] for i in signing.list_ids())
    with pytest.raises(ValueError, match="WRONG_PASSWORD"):
        signing.load_signer(ident["id"], "pogresno")
    signer = signing.load_signer(ident["id"], "tajna123")
    assert signing.signer_name(signer) == "Marko Đorđević"

    doc = make_doc(1, rotation=rot)
    page = doc[0]
    disp = (200, 250, 380, 320)
    box = signing.box_from_display(page, disp)
    bw, bh = box[2] - box[0], box[3] - box[1]
    ap = signing.appearance_pdf(bw, bh, lines=[("Digitalno potpisao:", 8, "#555555"), ("Marko Đorđević", 13, "#000000")],
                                image=png_bytes(300, 100, (0, 0, 0, 255), mark=False), rotate=page.rotation)
    out = os.path.join(tmp_path, "signed.pdf")
    field = signing.sign_pdf(doc.tobytes(), out, signer, page_index=0, box=box, appearance_pdf=ap,
                             reason="Odobreno", location="Sarpsborg")
    assert field == "Potpis1"
    res = _signed_ok(out)
    assert len(res) == 1 and res[0]["status"] == "valid", res
    assert res[0]["signer"] == "Marko Đorđević" and res[0]["reason"] == "Odobreno"
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", res[0]["time"])  # the UI formats it in its language

    # the appearance is drawn inside the box the user chose, upright on screen
    signed = pymupdf.open(out)
    sp = signed[0]
    pix = sp.get_pixmap(dpi=72)
    dark = [(x, y) for y in range(250, 320) for x in range(200, 380) if sum(pix.pixel(x, y)[:3]) < 200]
    assert len(dark) > 200
    outside = [(x, y) for y in range(330, 410) for x in range(200, 380) if sum(pix.pixel(x, y)[:3]) < 200]
    assert not outside
    # the signature image sits on the left part of the box on screen
    left = sum(1 for x, _ in dark if x < 260)
    assert left > len(dark) * 0.3


def test_sign_protected_and_tamper(tmp_path):
    ident = signing.create_id("Ana", "pw12345")
    signer = signing.load_signer(ident["id"], "pw12345")
    doc = make_doc(1)
    out = os.path.join(tmp_path, "s.pdf")
    prot = {"user_pw": "open1", "owner_pw": "own1", "perms": -1, "algorithm": "aes256"}
    signing.sign_pdf(doc.tobytes(), out, signer, box=None, protection=prot, reason="Odobreno", location="Oslo")
    d = pymupdf.open(out)
    assert d.needs_pass and d.authenticate("open1")
    res = _signed_ok(out, "open1")
    assert res and res[0]["status"] == "valid", res
    # in an encrypted file the texts used to come back as "<...DecryptedObjectProxy object at ...>"
    assert (res[0]["reason"], res[0]["location"]) == ("Odobreno", "Oslo")
    # flip one byte inside the signed range -> invalid
    data = bytearray(open(out, "rb").read())
    i = data.find(b"stream") + 20
    data[i] ^= 0xFF
    res = signing.validate(bytes(data), "open1")
    assert not res or res[0]["status"] != "valid"


def test_a_protected_copy_of_a_signed_file_has_a_broken_signature(tmp_path):
    """Changing the protection rewrites the whole file, so the UI asks first; the copy shows why."""
    ident = signing.create_id("Ana", "pw12345")
    signer = signing.load_signer(ident["id"], "pw12345")
    out = os.path.join(tmp_path, "s.pdf")
    signing.sign_pdf(make_doc(1).tobytes(), out, signer, box=None, reason="Odobreno", location="Oslo")
    enc = os.path.join(tmp_path, "s_protected.pdf")
    pt.save_pdf(pymupdf.open(out), enc, user_pw="open1", owner_pw="own1")
    res = _signed_ok(enc, "open1")
    assert res and res[0]["status"] == "invalid", res
    assert (res[0]["signer"], res[0]["reason"], res[0]["location"]) == ("Ana", "Odobreno", "Oslo")


def test_untrusted_and_import_remove(tmp_path):
    ident = signing.create_id("Treće Lice", "x12345")
    p12 = os.path.join(os.environ["AURORA_DATA_DIR"], "ids", ident["id"] + ".p12")
    signer = signing.load_signer(ident["id"], "x12345")
    out = os.path.join(tmp_path, "u.pdf")
    signing.sign_pdf(make_doc(1).tobytes(), out, signer)
    signing.remove_id(ident["id"])  # no longer one of "my" IDs -> identity not confirmed
    assert ident["id"] not in {i["id"] for i in signing.list_ids()}
    res = _signed_ok(out)
    assert res[0]["status"] == "untrusted", res
    removed = os.path.join(os.path.dirname(p12), "removed", os.path.basename(p12))
    assert os.path.exists(removed)  # nothing is deleted for good
    with pytest.raises(ValueError, match="WRONG_PASSWORD"):
        signing.import_id(removed, "bad")
    again = signing.import_id(removed, "x12345")
    assert again["name"] == "Treće Lice"


def test_a_damaged_id_file_does_not_hide_signatures(tmp_path):
    ident = signing.create_id("Ana", "pw12345")
    out = os.path.join(tmp_path, "s.pdf")
    signing.sign_pdf(make_doc(1).tobytes(), out, signing.load_signer(ident["id"], "pw12345"))
    broken = signing.ids_dir() / "broken.json"
    broken.write_text('{"cert_pem": "-----BEGIN', encoding="utf-8")  # cut off, e.g. by a power cut
    try:
        assert [s["status"] for s in _signed_ok(out)] == ["valid"]
    finally:
        broken.unlink()
