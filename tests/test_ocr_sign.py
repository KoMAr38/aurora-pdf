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


@pytest.mark.parametrize("rot", (90, 270))
def test_ocr_text_lands_on_the_word_of_a_cropped_turned_scan(rot):
    """A turned scan that was cropped: the text layer went in through ~page.transformation_matrix, which leaves
    out the CropBox position of a turned page, and sat a crop's width away from the words in the picture."""
    doc = scanned_doc(rot)
    w, h = doc[0].rect.width, doc[0].rect.height  # on screen, before the crop
    pt.crop_pages(doc, [0], (0.05, 0.05, 0.95, 0.95))
    page = doc[0]
    assert ocr.ocr_page(page, "srp_latn+eng", dpi=200) >= 10
    first = next(w for w in an.text_layer(page) if w[5].startswith("Ugovor"))
    assert first[4] == 0 and abs(first[0] - (120 * 72 / 150 - 0.05 * w)) < 6 and abs(first[1] - (200 * 72 / 150 - 0.05 * h)) < 8


def test_ocr_reads_the_scan_not_the_comments_on_it():
    """A comment on a scan is no text of the page: the page still needs OCR, the comment's words stay out of the
    text layer, and the layer brings no second copy of the picture into the file."""
    doc = scanned_doc()
    page = doc[0]
    an.add_freetext(page, (100, 600, 500, 700), "Napomena: proveriti iznos pre slanja na potpis direktoru")
    assert ocr.looks_scanned(doc) and not ocr.page_has_text(page)
    before = len(doc.tobytes(garbage=3, deflate=True))
    assert ocr.ocr_page(page, "srp_latn+eng", dpi=300) >= 10
    assert len(doc.tobytes(garbage=3, deflate=True)) - before < 30_000  # fonts and text; the picture took 120 kB
    for a in list(page.annots()):
        page.delete_annot(a)
    assert "saradnji" in page.get_text() and "Napomena" not in page.get_text()


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


_LOCK = {"user_pw": "open1", "owner_pw": "own1", "perms": -1, "algorithm": "aes256"}


def _signed_form(tmp_path, protection=None):
    """An invoice form with an amount field, signed by the app: (path, signer)."""
    doc = make_doc(1)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "amount", "1 000"
    w.rect = pymupdf.Rect(300, 86, 450, 106)
    doc[0].add_widget(w)
    ident = signing.create_id("Ola Nordmann", "pw12345")
    signer = signing.load_signer(ident["id"], "pw12345")
    out = os.path.join(tmp_path, "invoice.pdf")
    signing.sign_pdf(doc.tobytes(), out, signer, protection=protection)
    return out, signer


def _writer(data: bytes, password=None):
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    wr = IncrementalPdfFileWriter(io.BytesIO(data))
    if password:
        wr.encrypt(password)
    return wr


def _filled(data: bytes, value: str, password=None) -> bytes:
    """The amount filled in as an incremental update, which a plain signature allows."""
    from pyhanko.pdf_utils import generic

    wr = _writer(data, password)
    field = next(f for f in wr.root["/AcroForm"]["/Fields"] if f.get_object()["/T"] == "amount")
    field.get_object()["/V"] = generic.TextStringObject(value)
    wr.mark_update(field)
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def _redrawn(data: bytes, text: str, password=None) -> bytes:
    """The amount drawn anew, its value left as it was: readers show the drawing."""
    from pyhanko.pdf_utils import generic

    wr = _writer(data, password)
    field = next(f for f in wr.root["/AcroForm"]["/Fields"] if f.get_object()["/T"] == "amount")
    face = generic.StreamObject({generic.NameObject("/Type"): generic.NameObject("/XObject"),
                                 generic.NameObject("/Subtype"): generic.NameObject("/Form"),
                                 generic.NameObject("/BBox"): generic.ArrayObject(map(generic.NumberObject, (0, 0, 150, 20)))},
                                stream_data=f"BT /Helv 12 Tf 2 5 Td ({text}) Tj ET".encode())
    field.get_object()["/AP"] = generic.DictionaryObject({generic.NameObject("/N"): wr.add_object(face)})
    wr.mark_update(field)
    buf = io.BytesIO()
    wr.write(buf)
    return buf.getvalue()


def test_a_field_filled_in_after_signing_shows(tmp_path):
    """A plain signature allows filling in the form later, so pyHanko finds nothing wrong: the panel must still
    say that the content changed after signing (here the amount to pay), also when only the field's drawing
    changed. A second signature fills only its own field, which changes nothing."""
    out, signer = _signed_form(tmp_path)
    assert [s["status"] for s in _signed_ok(out)] == ["valid"]
    for changed in (_filled(open(out, "rb").read(), "9 000 000"), _redrawn(open(out, "rb").read(), "9 000 000")):
        res = signing.validate(changed)
        assert [(s["status"], s["detail"]) for s in res] == [("modified", "ENTIRE_REVISION; modification=FORM_FILLING")]
    twice = os.path.join(tmp_path, "twice.pdf")
    signing.sign_pdf(open(out, "rb").read(), twice, signer)
    assert [s["status"] for s in signing.validate(open(twice, "rb").read())] == ["valid", "valid"]


def test_a_protected_form_is_read_decrypted(tmp_path):
    """In a protected file the field names and values are encrypted, and pyHanko's .get() hands them back still
    encrypted: the second of two people signing their own prepared field showed the first signature as changed,
    and an amount shown on two pages (a field with two widgets) changed after signing showed as no change."""
    doc = make_doc(2)
    for name, r in (("ana", (50, 450, 200, 500)), ("bo", (220, 450, 370, 500))):
        w = pymupdf.Widget()
        w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_SIGNATURE, name, pymupdf.Rect(r)
        doc[0].add_widget(w)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "note", "betales innen 14 dager"
    w.rect = pymupdf.Rect(50, 520, 370, 540)
    doc[0].add_widget(w)
    amount, kids = doc.get_new_xref(), (doc.get_new_xref(), doc.get_new_xref())
    doc.update_object(amount, f"<</FT/Tx/T(amount)/DA(/Helv 12 Tf 0 g)/V(1 000)/Kids[{kids[0]} 0 R {kids[1]} 0 R]>>")
    for kid, pno in zip(kids, (0, 1)):
        doc.update_object(kid, f"<</Type/Annot/Subtype/Widget/Rect[300 500 400 520]/Parent {amount} 0 R/F 4/P {doc.page_xref(pno)} 0 R>>")
        annots = doc.xref_get_key(doc.page_xref(pno), "Annots")[1].strip("[]")
        doc.xref_set_key(doc.page_xref(pno), "Annots", f"[{annots} {kid} 0 R]")
    fields = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/Fields")[1].strip("[]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/Fields", f"[{fields} {amount} 0 R]")
    ident = signing.create_id("Ola Nordmann", "pw12345")
    signer = signing.load_signer(ident["id"], "pw12345")
    first, both = os.path.join(tmp_path, "ana.pdf"), os.path.join(tmp_path, "both.pdf")
    signing.sign_pdf(doc.tobytes(), first, signer, field_name="ana", protection=_LOCK)
    signing.sign_pdf(open(first, "rb").read(), both, signer, field_name="bo", password="open1")
    assert [s["status"] for s in signing.validate(open(both, "rb").read(), "open1")] == ["valid", "valid"]

    def more(wr, obj):
        from pyhanko.pdf_utils import generic
        obj["/V"] = generic.TextStringObject("9 000 000")

    wr = _writer(open(first, "rb").read(), "open1")
    ref = next(f for f in wr.root["/AcroForm"]["/Fields"] if f.get_object()["/T"] == "amount")
    more(wr, ref.get_object())
    wr.mark_update(ref)
    buf = io.BytesIO()
    wr.write(buf)
    assert [s["status"] for s in signing.validate(buf.getvalue(), "open1")] == ["modified"]


def _tsa():
    """A time-stamping authority of its own, one the app does not know."""
    import datetime as dt

    from asn1crypto import keys, x509 as ax509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
            .sign(key, hashes.SHA256()))
    tsa_cert = ax509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    tsa_key = keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    return DummyTimeStamper(tsa_cert=tsa_cert, tsa_key=tsa_key, certs_to_embed=[tsa_cert])


def test_long_term_data_after_signing_is_no_change_and_a_timestamp_is_no_signature(tmp_path):
    """Long-term (PAdES) files get validation data (DSS) and document timestamps after their signatures. On a
    form both once showed the signature as changed, and the timestamp as an invalid signature."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.validation import dss

    out, signer = _signed_form(tmp_path)
    wr = IncrementalPdfFileWriter(io.BytesIO(open(out, "rb").read()))
    dss.DocumentSecurityStore.supply_dss_in_writer(wr, sig_contents=None, certs=[signer.signing_cert])
    buf = io.BytesIO()
    wr.write(buf)
    assert [s["status"] for s in signing.validate(buf.getvalue())] == ["valid"]
    stamped = io.BytesIO()
    signers.PdfTimeStamper(_tsa()).timestamp_pdf(IncrementalPdfFileWriter(io.BytesIO(buf.getvalue())), "sha256",
                                                 output=stamped)
    res = signing.validate(stamped.getvalue())
    assert [(s["kind"], s["status"], s["signer"]) for s in res] == [
        ("signature", "valid", "Ola Nordmann"), ("timestamp", "untrusted", "Test TSA")]
    assert res[1]["time"]
    res = signing.validate(_filled(stamped.getvalue(), "9 000 000"))  # filled in after both
    assert [s["status"] for s in res] == ["modified", "modified"]


def test_a_locked_target_leaves_no_half_signed_copy(tmp_path, monkeypatch):
    ident = signing.create_id("Ola Nordmann", "pw12345")
    out = tmp_path / "locked.pdf"
    out.write_bytes(b"open in another program")

    def locked(src, dst):
        raise PermissionError(13, "in use", str(dst))

    monkeypatch.setattr(signing.os, "replace", locked)
    with pytest.raises(PermissionError):
        signing.sign_pdf(make_doc(1).tobytes(), str(out), signing.load_signer(ident["id"], "pw12345"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["locked.pdf"] and out.read_bytes() == b"open in another program"


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
