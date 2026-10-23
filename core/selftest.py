"""Installation check: python main.py --selftest

Runs every engine feature once on throw-away documents (render, encryption, watermark,
OCR, certificate signing, Word export, compression, the local UI server) and writes the result to
<data dir>/selftest.txt. Exit code 0 means everything works. Nothing touches the user's
documents, settings or Digital IDs: signing uses a temporary profile.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

from .paths import FONT_SANS, TESSDATA, WEB_DIR, data_dir


def _sample_doc():
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=420, height=595)
    page.insert_font(fontname="noto", fontfile=str(FONT_SANS))
    page.insert_text((50, 90), "Aurora selftest Čačak Tromsø", fontname="noto", fontsize=18)
    return doc


def check_imports() -> str:
    import logging

    import cryptography
    import PIL
    import pyhanko  # noqa: F401
    import pymupdf

    root = logging.getLogger()
    if not root.handlers:  # pdf2docx calls logging.basicConfig on import; keep its chatter off the console
        root.addHandler(logging.NullHandler())
    pymupdf.set_messages(pylogging=True)  # and its `import fitz` deprecation notice too
    import pdf2docx  # noqa: F401

    from importlib.metadata import version

    parts = [f"PyMuPDF {pymupdf.VersionBind}", f"pyHanko {version('pyHanko')}", f"cryptography {cryptography.__version__}",
             f"pdf2docx {version('pdf2docx')}", f"Pillow {PIL.__version__}"]
    try:
        import webview  # noqa: F401  pywebview 6 has no __version__, so the version comes from the package metadata

        parts.append(f"pywebview {version('pywebview')}")
    except Exception as exc:  # the window library is only needed for the desktop window
        if sys.platform.startswith("win"):
            raise
        parts.append(f"pywebview missing ({type(exc).__name__})")
    return ", ".join(parts)


def check_resources() -> str:
    from .ocr import available_languages

    needed = [WEB_DIR / "index.html", WEB_DIR / "styles.css", FONT_SANS]
    missing = [str(p) for p in needed if not p.exists()]
    js = sorted(p.name for p in (WEB_DIR / "js").glob("*.js"))
    if len(js) < 9:
        missing.append(f"web/js has only {js}")
    langs = available_languages()
    if not langs:
        missing.append(f"no OCR languages in {TESSDATA}")
    if missing:
        raise RuntimeError("missing: " + "; ".join(missing))
    return f"{len(js)} scripts, OCR languages: {', '.join(sorted(langs))}"


def check_render() -> str:
    from . import pdftools as pt

    doc = _sample_doc()
    t = time.perf_counter()
    jpg = pt.render_jpeg(doc[0], 1440)
    ms = (time.perf_counter() - t) * 1000
    if not jpg.startswith(b"\xff\xd8"):
        raise RuntimeError("not a JPEG")
    return f"1440 px page in {ms:.0f} ms ({len(jpg) // 1024} KB)"


def check_encryption(tmp: Path) -> str:
    from . import pdftools as pt

    out = tmp / "locked.pdf"
    pt.save_pdf(_sample_doc(), str(out), user_pw="open-1", owner_pw="owner-1", allowed=["print"])
    try:
        pt.load_for_editing(str(out), None)
        raise RuntimeError("opened without a password")
    except ValueError as exc:
        if str(exc) != "NEED_PASSWORD":
            raise
    doc, info = pt.load_for_editing(str(out), "open-1")
    if "selftest" not in doc[0].get_text() or info.get("owner") or not info.get("protected"):
        raise RuntimeError(f"unexpected result: {info}")
    return f"AES-256, open and owner passwords, permissions ({info.get('algorithm')})"


def check_watermark() -> str:
    from . import pdftools as pt

    doc = _sample_doc()
    pt.watermark_text(doc, "POVERLJIVO", pages=[0])  # drawn from the font's glyphs (fontTools)
    if not doc[0].get_drawings() or "POVERLJIVO" in doc[0].get_text():
        raise RuntimeError("watermark not drawn, or it came out as selectable text")
    return "text watermark"


def check_ocr() -> str:
    import pymupdf

    from . import ocr

    src = _sample_doc()
    pix = src[0].get_pixmap(dpi=150)
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=595)
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    t = time.perf_counter()
    words = ocr.ocr_page(page, "eng", dpi=200)
    text = page.get_text()
    if "selftest" not in text:
        raise RuntimeError(f"recognised {words} words but not the test word: {text!r}")
    return f"{words} words in {(time.perf_counter() - t) * 1000:.0f} ms"


def check_signing(tmp: Path) -> str:
    old = os.environ.get("AURORA_DATA_DIR")
    os.environ["AURORA_DATA_DIR"] = str(tmp / "profile")  # never touch the user's Digital IDs
    try:
        from . import signing

        info = signing.create_id("Aurora Selftest", "selftest-pw")
        signer = signing.load_signer(info["id"], "selftest-pw")
        out = tmp / "signed.pdf"
        signing.sign_pdf(_sample_doc().tobytes(), str(out), signer, reason="selftest")
        result = signing.validate(out.read_bytes())
        if not result or result[0]["status"] not in ("valid", "untrusted"):
            raise RuntimeError(f"validation: {result}")
        return f"signed and verified ({result[0]['status']})"
    finally:
        if old is None:
            os.environ.pop("AURORA_DATA_DIR", None)
        else:
            os.environ["AURORA_DATA_DIR"] = old


def check_docx(tmp: Path) -> str:
    from . import pdftools as pt

    src = tmp / "for-word.pdf"
    _sample_doc().save(str(src))
    out = tmp / "out.docx"
    pt.export_docx(str(src), str(out))
    if out.stat().st_size < 2000:
        raise RuntimeError("docx too small")
    return f"{out.stat().st_size // 1024} KB"


def check_compress(tmp: Path) -> str:
    import pymupdf

    from . import pdftools as pt

    doc = _sample_doc()
    big = doc[0].get_pixmap(dpi=200)
    doc[0].insert_image((50, 120, 370, 440), pixmap=big)  # a big picture to make smaller
    out = tmp / "small.pdf"
    before, after = pt.compress_to(doc, str(out))  # its image pass runs in a process of its own
    with pymupdf.open(out) as small:
        width = max(img[2] for img in small[0].get_images())
    if width >= big.width or not after < before:  # without the image pass the copy comes out smaller too
        raise RuntimeError(f"the image pass did not run: {big.width} -> {width} px, {before} -> {after} bytes")
    return f"{before // 1024} KB -> {after // 1024} KB"


def check_server() -> str:
    from api import Api

    from .server import AppServer
    from .session import Session

    session = Session()
    server = AppServer(Api(session), session, WEB_DIR).start()
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a system proxy would get 127.0.0.1 too
    try:
        html = local.open(server.url, timeout=10).read().decode("utf-8")
        if "js/app.js" not in html:
            raise RuntimeError("index page looks wrong")
        req = urllib.request.Request(server.url + "api/state", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        state = json.loads(local.open(req, timeout=10).read())
        if not state.get("ok"):
            raise RuntimeError(f"state: {state}")
        return "UI and API answer on 127.0.0.1"
    finally:
        server.stop()
        session.close()


def check_webview2() -> str:
    """The window needs Microsoft Edge WebView2 (preinstalled on Windows 10/11)."""
    if not sys.platform.startswith("win"):
        return "not Windows: skipped"
    import winreg

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return f"WebView2 {version}"
        except OSError:
            continue
    raise RuntimeError("Microsoft Edge WebView2 Runtime not found (https://go.microsoft.com/fwlink/p/?LinkId=2124703)")


def run() -> int:
    # its own folder, gone at the end: the check makes a Digital ID (.p12) and a protected file in it
    with tempfile.TemporaryDirectory(prefix="aurora_selftest_", ignore_cleanup_errors=True) as name:
        return _run(Path(name))


def _run(tmp: Path) -> int:
    checks = [
        ("imports", check_imports), ("resources", check_resources), ("render", check_render),
        ("encryption", lambda: check_encryption(tmp)), ("watermark", check_watermark), ("ocr", check_ocr),
        ("signing", lambda: check_signing(tmp)), ("word export", lambda: check_docx(tmp)),
        ("compression", lambda: check_compress(tmp)), ("ui server", check_server), ("webview2", check_webview2),
    ]
    lines = [f"Aurora PDF selftest  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Python {sys.version.split()[0]} on {platform.platform()}", ""]
    failed = 0
    for name, fn in checks:
        try:
            line = f"OK    {name:12s} {fn()}"
            lines.append(line)
        except Exception as exc:
            failed += 1
            line = f"FAIL  {name:12s} {type(exc).__name__}: {exc}"
            lines.append(line)
            lines.extend("      " + ln for ln in traceback.format_exc().rstrip().splitlines()[-6:])
        print(line, flush=True)
    lines += ["", "All checks passed." if not failed else f"{failed} check(s) failed."]
    report = data_dir() / "selftest.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1], f"Report: {report}")
    return 0 if not failed else 1
