"""Certificate-based digital signatures (pyHanko) and the user's Digital IDs.

A Digital ID is a PKCS#12 file (.p12/.pfx) holding a private key and its certificate.
IDs live in <data dir>/ids; a small JSON sidecar keeps the public details so the list
can be shown without asking for passwords. Signing always writes a NEW file: the
signature covers the exact bytes written, so the signed file is never rewritten.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .paths import data_dir

IDS_SUBDIR = "ids"


def ids_dir() -> Path:
    d = data_dir() / IDS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Digital IDs ----------------------------------------------------------------


def _describe(cert) -> dict:
    """Public details of a cryptography x509 certificate."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    def first(name, oid):
        attrs = name.get_attributes_for_oid(oid)
        return attrs[0].value if attrs else ""

    subj, iss = cert.subject, cert.issuer
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {
        "name": first(subj, NameOID.COMMON_NAME),
        "email": first(subj, NameOID.EMAIL_ADDRESS),
        "org": first(subj, NameOID.ORGANIZATION_NAME),
        "issuer": first(iss, NameOID.COMMON_NAME) or first(iss, NameOID.ORGANIZATION_NAME),
        "self_signed": subj == iss,
        "not_after": not_after.date().isoformat(),
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex()[:40],
    }


def _store(p12_bytes: bytes, cert) -> dict:
    from cryptography.hazmat.primitives import serialization

    ident = uuid.uuid4().hex[:12]
    folder = ids_dir()
    (folder / f"{ident}.p12").write_bytes(p12_bytes)
    meta = _describe(cert)
    meta.update(id=ident, created=datetime.date.today().isoformat(),
                cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode())
    (folder / f"{ident}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return _public(meta)


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != "cert_pem"}


def list_ids() -> list[dict]:
    out = []
    for f in sorted(ids_dir().glob("*.json")):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (ids_dir() / f"{meta.get('id')}.p12").exists():
            out.append(_public(meta))
    return out


def create_id(name: str, password: str, *, email: str = "", org: str = "", years: int = 5) -> dict:
    """Make a self-signed Digital ID (RSA-3072, SHA-256) protected by ``password``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    if len(password or "") < 4:
        raise ValueError("password too short")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if org.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org.strip()))
    if email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email.strip()))
    subject = x509.Name(attrs)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * years))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.EMAIL_PROTECTION,
                ObjectIdentifier("1.3.6.1.5.5.7.3.36"),      # id-kp-documentSigning
                ObjectIdentifier("1.3.6.1.4.1.311.10.3.12"),  # Microsoft document signing
            ]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if email.strip():
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email.strip())]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    p12 = pkcs12.serialize_key_and_certificates(
        name.encode("utf-8"), key, cert, None, BestAvailableEncryption(password.encode("utf-8"))
    )
    return _store(p12, cert)


def import_id(path: str, password: str) -> dict:
    """Copy an existing .pfx/.p12 into the ID store (checks the password first)."""
    from cryptography.hazmat.primitives.serialization import pkcs12

    data = Path(path).read_bytes()
    try:
        key, cert, _extra = pkcs12.load_key_and_certificates(data, (password or "").encode("utf-8"))
    except ValueError as exc:
        raise ValueError("WRONG_PASSWORD") from exc
    if key is None or cert is None:
        raise ValueError("file has no key or certificate")
    return _store(data, cert)


def remove_id(ident: str) -> None:
    """Move an ID out of the list (kept in ids/removed so nothing is lost for good)."""
    if not re.fullmatch(r"[0-9a-f]{12}", ident or ""):
        raise ValueError("bad id")
    folder = ids_dir()
    trash = folder / "removed"
    trash.mkdir(exist_ok=True)
    for ext in (".p12", ".json"):
        src = folder / f"{ident}{ext}"
        if src.exists():
            shutil.move(str(src), str(trash / src.name))


def _id_path(ident: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", ident or ""):
        raise ValueError("bad id")
    p = ids_dir() / f"{ident}.p12"
    if not p.exists():
        raise ValueError("ID not found")
    return p


def load_signer(ident: str, password: str):
    from cryptography.hazmat.primitives.serialization import pkcs12
    from pyhanko.sign import signers

    path = _id_path(ident)
    pw = (password or "").encode("utf-8")
    try:  # check the password first: pyHanko only logs a bad passphrase and returns None
        pkcs12.load_key_and_certificates(path.read_bytes(), pw)
    except ValueError as exc:
        raise ValueError("WRONG_PASSWORD") from exc
    signer = signers.SimpleSigner.load_pkcs12(pfx_file=str(path), passphrase=pw)
    if signer is None:
        raise ValueError("WRONG_PASSWORD")
    return signer


def signer_name(signer) -> str:
    try:
        return signer.signing_cert.subject.native.get("common_name") or signer.signing_cert.subject.human_friendly
    except Exception:
        return "?"


# --- signing --------------------------------------------------------------------


def sign_pdf(
    pdf_bytes: bytes,
    out_path: str,
    signer,
    *,
    page_index: int = 0,
    box: tuple[float, float, float, float] | None = None,
    appearance_pdf: bytes | None = None,
    reason: str = "",
    location: str = "",
    contact: str = "",
    protection: dict | None = None,
    field_name: str | None = None,
    password: str | None = None,
) -> str:
    """Sign ``pdf_bytes`` into ``out_path``, always as an incremental update on top of them.

    ``box`` is the visible signature rectangle in PDF user space (None = invisible);
    ``appearance_pdf`` is a one-page PDF drawn into that box. ``protection`` encrypts plain
    bytes first. ``password`` instead says the bytes are an existing, already encrypted file
    (e.g. one with earlier signatures, which must stay byte for byte). Returns the field name.
    """
    from pyhanko import stamp
    from pyhanko.pdf_utils.crypt.permissions import StandardPermissions
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.pdf_utils.writer import copy_into_new_writer
    from pyhanko.sign import fields, signers

    data = pdf_bytes
    auth_pw = password  # password pyHanko authenticates with when appending the signature
    if protection and password is None:
        user_pw = protection.get("user_pw") or ""
        owner_pw = protection.get("owner_pw") or user_pw
        if not owner_pw:
            raise ValueError("owner password unknown")
        auth_pw = owner_pw
        w = copy_into_new_writer(PdfFileReader(io.BytesIO(data)))
        perms = protection.get("perms")
        kw = {"pdf_mac": False}
        if perms is not None and perms != -1:
            kw["perms"] = StandardPermissions.from_uint(perms & 0xFFFFFFFF)
        w.encrypt(owner_pw, user_pw, **kw)
        buf = io.BytesIO()
        w.write(buf)
        data = buf.getvalue()

    existing, empty = set(), set()
    try:
        reader = PdfFileReader(io.BytesIO(data))
        if auth_pw is not None:
            reader.decrypt(auth_pw)
        for name, value, _ref in fields.enumerate_sig_fields(reader):
            (existing if value is not None else empty).add(name)
    except Exception:
        pass
    use_empty = bool(field_name) and field_name in empty  # sign into the form's own empty field
    if not use_empty:
        field_name = None
    if not field_name:
        k = 1
        while f"Potpis{k}" in existing:
            k += 1
        field_name = f"Potpis{k}"

    tmp_ap = None
    try:
        style = None
        if box and appearance_pdf:
            fd, tmp_ap = tempfile.mkstemp(suffix=".pdf")
            with os.fdopen(fd, "wb") as f:
                f.write(appearance_pdf)
            style = stamp.StaticStampStyle.from_pdf_file(tmp_ap, border_width=0)
        writer = IncrementalPdfFileWriter(io.BytesIO(data))
        if auth_pw is not None:
            writer.encrypt(auth_pw)
        if not use_empty:
            spec = fields.SigFieldSpec(field_name, on_page=page_index, box=box) if box else fields.SigFieldSpec(field_name, on_page=page_index)
            fields.append_signature_field(writer, spec)
        meta = signers.PdfSignatureMetadata(
            field_name=field_name, reason=reason or None, location=location or None,
            contact_info=contact or None, name=signer_name(signer), md_algorithm="sha256",
        )
        pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style or stamp.TextStampStyle(stamp_text="%(signer)s\n%(ts)s", border_width=1))
        out = io.BytesIO()
        pdf_signer.sign_pdf(writer, output=out)
        tmp_out = out_path + ".aurora-tmp"
        with open(tmp_out, "wb") as f:
            f.write(out.getvalue())
        os.replace(tmp_out, out_path)
        return field_name
    finally:
        if tmp_ap:
            try:
                os.remove(tmp_ap)
            except OSError:
                pass


# --- validation -----------------------------------------------------------------


def _own_trust_roots() -> list:
    from asn1crypto import pem, x509 as ax509

    roots = []
    for f in ids_dir().glob("*.json"):
        try:  # one damaged file must not hide the signatures in the document
            meta = json.loads(f.read_text(encoding="utf-8"))
            _, _, der = pem.unarmor(meta["cert_pem"].encode())
            roots.append(ax509.Certificate.load(der))
        except Exception:
            continue
    return roots


def validate(pdf_bytes: bytes, password: str | None = None) -> list[dict]:
    """Check every signature in a file. Never raises; problems become a status."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        if reader.encrypted:
            reader.decrypt(password or "")
        sigs = list(reader.embedded_signatures)
    except Exception:
        return []
    roots = _own_trust_roots()
    out = []
    for sig in sigs:
        item = {"field": sig.field_name, "signer": "", "email": "", "issuer": "", "time": "",
                "reason": "", "location": "", "status": "invalid", "detail": ""}
        try:
            obj = sig.sig_object
            # in a protected file .get() hands back pyHanko's still-encrypted proxy; [] decrypts
            item["reason"] = str(obj["/Reason"]) if "/Reason" in obj else ""
            item["location"] = str(obj["/Location"]) if "/Location" in obj else ""
            cert = sig.signer_cert
            native = cert.subject.native
            item["signer"] = native.get("common_name") or cert.subject.human_friendly
            item["email"] = native.get("email_address", "") or ""
            item["issuer"] = cert.issuer.native.get("common_name", "") or cert.issuer.human_friendly
            item["self_signed"] = cert.subject == cert.issuer
            ts = sig.self_reported_timestamp
            item["time"] = ts.astimezone().isoformat(timespec="minutes") if ts else ""
            vc = ValidationContext(trust_roots=roots, allow_fetching=False)
            st = validate_pdf_signature(sig, vc)
            covered = st.coverage.name == "ENTIRE_FILE"
            item["detail"] = f"{st.coverage.name}; modification={getattr(st.modification_level, 'name', '')}"
            if not (st.intact and st.valid):
                item["status"] = "invalid"
            elif not covered and not st.docmdp_ok:  # later updates the signature does not allow
                item["status"] = "modified"
            elif st.trusted:
                item["status"] = "valid"
            else:
                item["status"] = "untrusted"
        except Exception as exc:  # malformed signature dictionaries, unsupported algorithms...
            item["detail"] = type(exc).__name__
        out.append(item)
    return out


def box_from_display(page, rect) -> tuple[float, float, float, float]:
    """Display-space rect on ``page`` -> signature box in PDF user space (x0, y0, x1, y1)."""
    import pymupdf

    r = pymupdf.Rect(rect) * page.derotation_matrix * ~page.transformation_matrix
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def appearance_pdf(width: float, height: float, *, lines: list[tuple[str, float, str]], image: bytes | None = None,
                   rotate: int = 0) -> bytes:
    """One-page PDF used as the visible signature: optional signature image on the left,
    text lines (text, size, hex colour) on the right. ``rotate`` (the page /Rotate) turns
    the drawing so it reads upright on rotated pages."""
    import pymupdf

    from .geom import rgb
    from .pdftools import FONT_NAME, FONT_SANS

    draw_w, draw_h = (height, width) if rotate in (90, 270) else (width, height)
    src = pymupdf.open()
    pg = src.new_page(width=draw_w, height=draw_h)
    pad = min(6.0, draw_h * 0.08)
    text_x = pad
    if image:
        img_w = min(draw_w * 0.45, draw_h * 2.2)
        pg.insert_image(pymupdf.Rect(pad, pad, pad + img_w, draw_h - pad), stream=image, keep_proportion=True)
        text_x = pad + img_w + pad
    total = sum(size * 1.25 for _, size, _ in lines) or 1
    scale = min(1.0, (draw_h - 2 * pad) / total)
    y = (draw_h - total * scale) / 2
    for text, size, color in lines:
        s = size * scale
        y += s * 1.05
        pg.insert_text((text_x, y), text, fontsize=s, fontname=FONT_NAME, fontfile=str(FONT_SANS), color=rgb(color))
        y += s * 0.2
    if rotate in (90, 180, 270):
        out = pymupdf.open()
        tp = out.new_page(width=width, height=height)
        tp.show_pdf_page(tp.rect, src, 0, rotate=rotate)  # same rule as the OCR overlay
        src.close()
        src = out
    src.subset_fonts()
    data = src.tobytes(garbage=3, deflate=True)
    src.close()
    return data
