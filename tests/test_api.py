"""Backend API and HTTP server, driven the way the interface drives them.

Native dialogs are replaced by a scripted queue (Dialogs.script), everything else is the
production code path: Api.dispatch -> Session -> engine, and real HTTP for the server.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import pymupdf
import pytest

from api import DOCX_TYPES, PATTERNS, PDF_TYPES, Api, Dialogs
from conftest import make_doc, png_bytes, save_tmp
from core import ocr, signing
from core import pdftools as pt
from core.server import AppServer
from core.session import Session
from core.paths import WEB_DIR


@pytest.fixture
def api():
    a = Api(Session(), Dialogs(), test_mode=True)
    a._dialogs.script = []
    yield a
    a.s.close()


def call(api, _method, **payload):
    return api.dispatch(_method, payload)


def answers(api, *items):
    api._dialogs.script = list(items)


def ok(res):
    assert res.get("ok"), res
    return res


def wait_job(api, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call(api, "job_status", id=job_id)["job"]
        if st["state"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("job timed out")


def opened(api, tmp_path, pages=3, rot=0, name="doc.pdf"):
    path = save_tmp(make_doc(pages, rotation=rot), tmp_path, name)
    return ok(call(api, "open_path", path=path)), path


# --- open / save / undo ---------------------------------------------------------------


def test_open_edit_undo_redo_save(api, tmp_path):
    res, path = opened(api, tmp_path)
    doc = res["doc"]
    assert doc["open"] and len(doc["pages"]) == 3 and not doc["dirty"]
    revs0 = [p[2] for p in doc["pages"]]

    d = ok(call(api, "rotate", pages=[0], degrees=90))["doc"]
    assert d["dirty"] and d["undo"] == "rotate"
    assert d["pages"][0][2] != revs0[0] and [p[2] for p in d["pages"][1:]] == revs0[1:]
    assert d["pages"][0][0] > d["pages"][0][1]  # width/height swapped on screen

    d = ok(call(api, "undo"))["doc"]
    assert [p[2] for p in d["pages"]] == revs0 and not d["dirty"]  # back to the saved state
    d = ok(call(api, "redo"))["doc"]
    assert d["dirty"] and d["pages"][0][2] != revs0[0]

    d = ok(call(api, "save"))["doc"]
    assert not d["dirty"]
    assert pymupdf.open(path)[0].rotation == 90


def test_errors_are_codes(api, tmp_path):
    assert call(api, "rotate", pages=[0])["error"] == "NO_DOC"
    assert call(api, "nonexistent")["error"] == "NOT_FOUND"
    opened(api, tmp_path)
    assert call(api, "delete_pages", pages=[0, 1, 2])["error"] == "CANNOT_DELETE_ALL"
    assert call(api, "annot_add", page=0, rev=999999, kind="note", point=[10, 10])["error"] == "STALE"
    assert call(api, "open_path", path=str(tmp_path / "missing.pdf"))["error"] == "FILE_NOT_FOUND"


def test_failed_edit_rolls_back(api, tmp_path):
    res, _ = opened(api, tmp_path)
    revs = [p[2] for p in res["doc"]["pages"]]
    bad = call(api, "reorder", order=[0, 0, 1])  # not a permutation
    assert not bad["ok"]
    d = call(api, "state")["doc"]
    assert [p[2] for p in d["pages"]] == revs and not d["dirty"] and d["undo"] == ""


def test_structural_edits_keep_revisions(api, tmp_path):
    res, _ = opened(api, tmp_path, pages=4)
    revs = [p[2] for p in res["doc"]["pages"]]
    d = ok(call(api, "reorder", order=[3, 2, 1, 0]))["doc"]
    assert [p[2] for p in d["pages"]] == revs[::-1]  # same pictures, new order: nothing re-renders
    d = ok(call(api, "delete_pages", pages=[0]))["doc"]
    assert [p[2] for p in d["pages"]] == revs[::-1][1:]
    d = ok(call(api, "insert_blank", at=1))["doc"]
    assert len(d["pages"]) == 4 and d["pages"][0][2] == revs[2] and d["pages"][1][2] not in revs
    d = ok(call(api, "duplicate", pages=[0]))["doc"]
    assert len(d["pages"]) == 5 and len({p[2] for p in d["pages"]}) == 5


def test_save_as_and_recent(api, tmp_path):
    opened(api, tmp_path)
    out = str(tmp_path / "copy.pdf")
    answers(api, out)
    d = ok(call(api, "save_as"))
    assert d["path"] == out and d["doc"]["name"] == "copy.pdf" and os.path.exists(out)
    st = call(api, "state")
    assert st["recent"][0]["path"] == os.path.abspath(out)
    answers(api, None)
    assert call(api, "save_as")["error"] == "CANCELLED"


# --- protection -------------------------------------------------------------------------


def test_protect_copy_and_reopen(api, tmp_path):
    _, src = opened(api, tmp_path)
    out = str(tmp_path / "locked.pdf")
    answers(api, out)
    ok(call(api, "save_protected", user_pw="tajna", owner_pw="gazda", allowed=["print"], algorithm="aes256"))
    assert pymupdf.open(out).needs_pass and not pymupdf.open(src).needs_pass
    r = call(api, "open_path", path=out)
    assert r["error"] == "NEED_PASSWORD" and r["name"] == "locked.pdf"
    assert call(api, "open_path", path=out, password="x")["error"] == "WRONG_PASSWORD"
    d = ok(call(api, "open_path", path=out, password="tajna"))["doc"]
    assert d["protected"] and not d["owner"] and d["rights"]["print"] and not d["rights"]["modify"]
    assert call(api, "rotate", pages=[0], degrees=90)["error"] == "NOT_ALLOWED"
    assert call(api, "unlock_owner", password="nope")["error"] == "WRONG_PASSWORD"
    d = ok(call(api, "unlock_owner", password="gazda"))["doc"]
    assert d["owner"] and d["rights"]["modify"]
    ok(call(api, "rotate", pages=[0], degrees=90))
    ok(call(api, "save"))  # keeps both passwords
    doc, info = pt.load_for_editing(out, "tajna")
    assert doc[0].rotation == 90 and not info["owner"]
    _, info = pt.load_for_editing(out, "gazda")
    assert info["owner"]


def test_owner_unknown_forces_copy(api, tmp_path):
    locked = str(tmp_path / "l.pdf")
    pt.save_pdf(make_doc(1), locked, user_pw="", owner_pw="boss", allowed=["print", "annotate", "modify"])
    d = ok(call(api, "open_path", path=locked))["doc"]
    assert d["protected"] and not d["owner"] and d["rights"]["modify"]
    ok(call(api, "annot_add", page=0, kind="note", point=[100, 100], text="x"))
    assert call(api, "save")["error"] == "SAVE_AS_COPY"
    out = str(tmp_path / "l2.pdf")
    answers(api, out)
    ok(call(api, "save_as"))
    _, info = pt.load_for_editing(out)
    assert info["protected"] and not info["owner"] and not pt.rights_for(info)["copy"]
    assert call(api, "save_unprotected")["error"] == "NOT_ALLOWED"


def test_unprotected_copy(api, tmp_path):
    locked = str(tmp_path / "l.pdf")
    pt.save_pdf(make_doc(1), locked, user_pw="pw", owner_pw="pw")
    ok(call(api, "open_path", path=locked, password="pw"))
    out = str(tmp_path / "open.pdf")
    answers(api, out)
    ok(call(api, "save_unprotected"))
    assert not pymupdf.open(out).needs_pass


# --- annotations, forms, text -------------------------------------------------------------


def test_annotation_round_trip(api, tmp_path):
    res, _ = opened(api, tmp_path, rot=90)
    rev = res["doc"]["pages"][0][2]
    d = ok(call(api, "annot_add", page=0, rev=rev, kind="ink", strokes=[[[100, 100], [140, 120], [180, 105]]],
                color="#ff0000", width=3))
    xref = d["xref"]
    rev2 = d["doc"]["pages"][0][2]
    assert rev2 != rev
    info = json.loads(api_server_info(api, rev2))
    assert any(a["x"] == xref and a["t"] == "Ink" for a in info["annots"])
    ok(call(api, "annot_move", page=0, xref=xref, dx=20, dy=10))
    words = [i for i, w in enumerate(info["words"]) if w[5] in ("lorem", "ipsum")]
    ok(call(api, "annot_add", page=0, kind="highlight", words=words))
    ok(call(api, "annot_add", page=0, kind="text", rect=[60, 200, 300, 240], text="Zdravo Đorđe"))
    ok(call(api, "annot_add", page=0, kind="note", point=[300, 60], text="Napomena"))
    sig = base64.b64encode(png_bytes(300, 100)).decode()
    sid = ok(call(api, "sig_save", png="data:image/png;base64," + sig))["id"]
    d = ok(call(api, "annot_add", page=0, kind="image", rect=[200, 250, 350, 300], sig=sid))
    items = ok(call(api, "annots_list"))["items"]
    assert {i["t"] for i in items} == {"Ink", "Highlight", "FreeText", "Text", "Stamp"}
    ok(call(api, "annot_resize", page=0, xref=d["xref"], rect=[200, 250, 380, 310]))
    ok(call(api, "annot_edit", page=0, xref=xref, color="#00ff00"))
    ok(call(api, "annot_delete", page=0, xref=xref))
    assert len(ok(call(api, "annots_list"))["items"]) == 4
    for _ in range(9):
        ok(call(api, "undo"))
    assert ok(call(api, "annots_list"))["items"] == []


def api_server_info(api, rev):
    srv = AppServer(api, api.s, WEB_DIR)
    return srv.page_info(rev)


def test_forms_through_api(api, tmp_path):
    doc = make_doc(1)
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.rect = "ime", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(50, 400, 250, 425)
    doc[0].add_widget(w)
    path = save_tmp(doc, tmp_path, "form.pdf")
    d = ok(call(api, "open_path", path=path))["doc"]
    assert d["forms"]
    info = json.loads(api_server_info(api, d["pages"][0][2]))
    f = info["fields"][0]
    ok(call(api, "field_set", page=0, xref=f["x"], value="Marko"))
    ok(call(api, "flatten"))
    d = call(api, "state")["doc"]
    assert not d["forms"] and "Marko" in call(api, "page_text", page=0)["text"]


def test_search_toc_metadata_attachments(api, tmp_path):
    opened(api, tmp_path)
    hits = ok(call(api, "search", query="marker-2"))["hits"]
    assert len(hits) == 1 and hits[0]["p"] == 1
    ok(call(api, "toc_set", items=[[1, "Uvod", 0], [2, "Detalj", 1]]))
    assert ok(call(api, "toc"))["items"] == [[1, "Uvod", 0, 0], [2, "Detalj", 1, 1]]
    ok(call(api, "set_metadata", title="Naslov", author="Marko"))
    info = ok(call(api, "doc_info"))
    assert info["meta"]["title"] == "Naslov" and info["pages"] == 3
    f = tmp_path / "a.txt"
    f.write_text("x")
    answers(api, [str(f)])
    assert len(ok(call(api, "attachment_add_dialog"))["items"]) == 1


def test_watermark_header_redaction(api, tmp_path):
    opened(api, tmp_path, rot=90)
    ok(call(api, "watermark_text", text="TAJNO", pages="1-2", angle=30, size=40, opacity=0.2))
    ok(call(api, "header_footer", text="{n}/{total}", position="bc"))
    assert "TAJNO" in call(api, "page_text", page=0)["text"]
    assert "TAJNO" not in call(api, "page_text", page=2)["text"]
    d = ok(call(api, "redact_search", query="lorem"))
    assert d["count"] == 3 and call(api, "redact_count")["count"] == 3
    assert ok(call(api, "redact_apply"))["count"] == 3
    assert "lorem" not in call(api, "page_text", page=1)["text"]
    assert call(api, "redact_apply")["error"] == "NOTHING_FOUND"


def test_page_tools_with_dialogs(api, tmp_path):
    opened(api, tmp_path, pages=4)
    other = save_tmp(make_doc(2), tmp_path, "other.pdf")
    answers(api, [other])
    d = ok(call(api, "insert_file_dialog", at=1))
    assert d["count"] == 2 and len(d["doc"]["pages"]) == 6
    img = tmp_path / "i.png"
    img.write_bytes(png_bytes())
    answers(api, [str(img), str(img)])
    assert ok(call(api, "insert_images_dialog", at=0))["count"] == 2
    ok(call(api, "crop", pages=[0], frac=[0.1, 0.1, 0.9, 0.9]))
    ok(call(api, "uncrop", pages=[0]))
    out = str(tmp_path / "ext.pdf")
    answers(api, out)
    ok(call(api, "extract_dialog", pages="1-2"))
    assert len(pymupdf.open(out)) == 2
    folder = tmp_path / "split"
    folder.mkdir()
    answers(api, str(folder))
    assert ok(call(api, "split_dialog", every=3))["count"] == 3


def test_merge_and_images_to_pdf(api, tmp_path):
    a = save_tmp(make_doc(2), tmp_path, "a.pdf")
    b = save_tmp(make_doc(3), tmp_path, "b.pdf")
    answers(api, [a, b])
    files = ok(call(api, "merge_dialog"))
    assert files["names"] == ["a.pdf", "b.pdf"]
    out = str(tmp_path / "m.pdf")
    answers(api, out)
    assert ok(call(api, "merge_run", paths=[b, a]))["pages"] == 5
    img = tmp_path / "i.png"
    img.write_bytes(png_bytes())
    answers(api, [str(img)], str(tmp_path / "img.pdf"))
    assert ok(call(api, "images_to_pdf_dialog"))["pages"] == 1


# --- jobs -----------------------------------------------------------------------------------


def test_exports_as_jobs(api, tmp_path):
    opened(api, tmp_path)
    out = str(tmp_path / "t.txt")
    answers(api, out)
    st = wait_job(api, ok(call(api, "export_start", kind="txt"))["job"])
    assert st["state"] == "done" and "Čačak" in open(out, encoding="utf-8").read()
    folder = tmp_path / "imgs"
    folder.mkdir()
    answers(api, str(folder))
    st = wait_job(api, ok(call(api, "export_start", kind="images", format="jpg", dpi=50))["job"])
    assert st["state"] == "done" and st["result"]["count"] == 3
    small = str(tmp_path / "small.pdf")
    answers(api, small)
    st = wait_job(api, ok(call(api, "export_start", kind="compress"))["job"])
    assert st["state"] == "done" and os.path.exists(small)


def test_docx_job(api, tmp_path):
    pytest.importorskip("pdf2docx")
    opened(api, tmp_path, pages=2)
    out = str(tmp_path / "w.docx")
    answers(api, out)
    st = wait_job(api, ok(call(api, "export_start", kind="docx"))["job"])
    assert st["state"] == "done" and os.path.getsize(out) > 1000


def test_ocr_job_is_one_undo_step(api, tmp_path):
    from test_ocr_sign import scanned_doc

    doc = scanned_doc(0)
    doc.insert_pdf(scanned_doc(90))
    path = save_tmp(doc, tmp_path, "scan.pdf")
    d = ok(call(api, "open_path", path=path))["doc"]
    assert d["scanned"]
    assert call(api, "rotate", pages=[0])["ok"]  # an edit before OCR
    res = ok(call(api, "ocr_start", langs="srp_latn+eng", dpi=200))
    st = wait_job(api, res["job"])
    assert st["state"] == "done" and st["result"]["pages"] == 2, st
    assert "saradnji" in call(api, "page_text", page=1)["text"]
    d = call(api, "state")["doc"]
    assert not d["scanned"] and d["undo"] == "ocr"
    ok(call(api, "undo"))
    assert "saradnji" not in call(api, "page_text", page=1)["text"]
    assert call(api, "ocr_start", langs="eng", pages="1")["ok"]  # works again after undo


# --- digital signatures ---------------------------------------------------------------------


def test_digital_signing_flow(api, tmp_path):
    opened(api, tmp_path, rot=0)
    ident = ok(call(api, "id_create", name="Marko Test", password="pw1234", email="m@x.no"))["id"]
    assert any(i["id"] == ident for i in ok(call(api, "ids_list"))["items"])
    out = str(tmp_path / "signed.pdf")
    assert call(api, "sign_digital", id=ident, password="bad")["error"] == "WRONG_PASSWORD"
    answers(api, out)
    d = ok(call(api, "sign_digital", id=ident, password="pw1234", page=0, rect=[200, 300, 380, 360],
                reason="Odobreno", location="Sarpsborg", labels={"signedBy": "Digitalno potpisao"}))
    assert d["path"] == out and d["doc"]["signed"] and d["doc"]["path"] == os.path.abspath(out)
    items = ok(call(api, "signatures_validate"))["items"]
    assert len(items) == 1 and items[0]["status"] == "valid" and items[0]["signer"] == "Marko Test"



def _signed_file(tmp_path, name, protection=None):
    ident = signing.create_id("Ana Første", "pw1234")
    out = str(tmp_path / name)
    signing.sign_pdf(make_doc(2).tobytes(), out, signing.load_signer(ident["id"], "pw1234"), protection=protection)
    return out


def _statuses(path, password=None):
    with open(path, "rb") as f:
        return [s["status"] for s in signing.validate(f.read(), password)]


def test_unchanged_signed_file_keeps_its_signature(api, tmp_path):
    f = _signed_file(tmp_path, "contract.pdf")
    assert ok(call(api, "open_path", path=f))["doc"]["signed"]
    ok(call(api, "save"))  # Ctrl+S with nothing changed
    copy = str(tmp_path / "copy.pdf")
    answers(api, copy)
    ok(call(api, "save_as"))
    assert _statuses(f) == ["valid"] and _statuses(copy) == ["valid"]


@pytest.mark.parametrize("protected", (False, True))
def test_counter_signature_keeps_the_first_one(api, tmp_path, protected):
    prot = {"user_pw": "open1", "owner_pw": "owner1", "perms": -1, "algorithm": "aes256"} if protected else None
    f = _signed_file(tmp_path, "contract.pdf", prot)
    assert ok(call(api, "open_path", path=f, password="open1" if protected else None))["doc"]["signed"]
    second = ok(call(api, "id_create", name="Bo Andre", password="pw5678"))["id"]
    out = str(tmp_path / "countersigned.pdf")
    answers(api, out)
    d = ok(call(api, "sign_digital", id=second, password="pw5678", page=0, rect=[200, 300, 380, 360]))["doc"]
    assert d["path"] == os.path.abspath(out)
    assert _statuses(out, "open1" if protected else None) == ["valid", "valid"]


def test_keep_protection_asks_only_for_the_open_password(api, tmp_path):
    path = str(tmp_path / "policy.pdf")
    pt.save_pdf(make_doc(2), path, user_pw="open1", owner_pw="owner1", allowed=["print"])
    ok(call(api, "open_path", path=path, password="owner1"))  # the owner opens it
    ok(call(api, "rotate", pages=[0]))
    assert call(api, "save")["error"] == "NEED_PASSWORDS"
    assert call(api, "set_protection", user_pw="typo")["error"] == "WRONG_PASSWORD"
    ok(call(api, "set_protection", user_pw="open1"))
    ok(call(api, "save"))
    _, info = pt.load_for_editing(path, "open1")
    assert not info["owner"] and pt.rights_for(info)["print"] and not pt.rights_for(info)["modify"]
    assert pt.load_for_editing(path, "owner1")[1]["owner"]


def test_non_owner_cannot_change_the_protection(api, tmp_path):
    path = str(tmp_path / "restricted.pdf")
    pt.save_pdf(make_doc(2), path, user_pw="user1", owner_pw="owner1", allowed=["print"])
    ok(call(api, "open_path", path=path, password="user1"))
    answers(api, str(tmp_path / "mine.pdf"))
    assert call(api, "save_protected", user_pw="x", owner_pw="mine", allowed=list(pt.ALL_PERMS))["error"] == "NOT_ALLOWED"


def test_failed_save_as_leaves_the_original_alone(api, tmp_path):
    path = str(tmp_path / "restricted.pdf")
    pt.save_pdf(make_doc(2), path, user_pw="user1", owner_pw="owner1", allowed=["print", "annotate"])
    ok(call(api, "open_path", path=path, password="user1"))
    ok(call(api, "annot_add", page=0, kind="note", point=[50, 50], text="hei"))
    answers(api, str(tmp_path / "no_such_dir" / "copy.pdf"))
    assert not call(api, "save_as")["ok"]
    assert call(api, "save")["error"] == "SAVE_AS_COPY"  # still guarded
    answers(api, path)  # the original picked in the Save As dialog
    assert call(api, "save_as")["error"] == "SAVE_AS_COPY"
    assert pt.load_for_editing(path, "owner1")[1]["owner"]


def test_exports_never_overwrite_files(api, tmp_path):
    folder = tmp_path / "Bilder"
    folder.mkdir()
    (folder / "scan_1.png").write_bytes(b"my own photo")
    ok(call(api, "open_path", path=save_tmp(make_doc(2), tmp_path, "scan.pdf")))
    answers(api, str(folder))
    assert wait_job(api, ok(call(api, "export_start", kind="images"))["job"])["state"] == "done"
    assert (folder / "scan_1.png").read_bytes() == b"my own photo"
    assert (folder / "scan_1 (2).png").exists() and (folder / "scan_2.png").exists()


def test_open_and_close_wait_for_a_running_ocr(api, tmp_path):
    from test_ocr_sign import scanned_doc

    doc = scanned_doc(0)
    for _ in range(3):
        doc.insert_pdf(scanned_doc(0))
    ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "scan.pdf")))
    other = save_tmp(make_doc(1), tmp_path, "other.pdf")
    job = ok(call(api, "ocr_start", langs="eng", dpi=150))["job"]
    assert call(api, "open_path", path=other)["error"] == "BUSY"
    assert call(api, "close_doc")["error"] == "BUSY"
    st = wait_job(api, job)
    assert st["state"] == "done" and st["doc"]["undo"] == "ocr" and st["doc"]["dirty"]


def test_failed_ocr_still_reports_the_pages_it_changed(api, tmp_path, monkeypatch):
    res, _ = opened(api, tmp_path)
    revs = [p[2] for p in res["doc"]["pages"]]
    seen = []

    def flaky(page, langs, dpi):
        seen.append(page.number)
        if len(seen) == 3:
            raise RuntimeError("tesseract crashed")
        return 1

    monkeypatch.setattr(ocr, "ocr_page", flaky)
    st = wait_job(api, ok(call(api, "ocr_start", langs="eng", force=True))["job"])
    assert st["state"] == "error"
    new = [p[2] for p in st["doc"]["pages"]]  # the viewer learns the new revisions of pages 1-2
    assert new[0] != revs[0] and new[1] != revs[1] and new[2] == revs[2]


def test_redaction_of_a_text_selection_is_one_step(api, tmp_path):
    opened(api, tmp_path)
    ok(call(api, "annot_add", page=0, kind="redact", rects=[[50, 60, 90, 75], [95, 60, 140, 75], [145, 60, 190, 75]]))
    assert call(api, "redact_count")["count"] == 3
    ok(call(api, "undo"))
    assert call(api, "redact_count")["count"] == 0


# the check pywebview 6.2 runs on every filter before a native dialog opens (webview.util.parse_file_type)
FILTER = r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$"


@pytest.mark.parametrize("lang", ["sr", "en", "no"])
def test_dialog_filters_pass_the_pywebview_check(api, lang, monkeypatch):
    """The tests answer dialogs from a script, so a filter pywebview rejects only showed up on Windows."""
    monkeypatch.setattr(api, "_lang", lambda: lang)
    filters = [*PDF_TYPES, *DOCX_TYPES, *(f for kind in PATTERNS for f in api._types(kind))]
    try:
        from webview.util import parse_file_type
    except ImportError:  # no window library here (Linux CI): same pattern
        def parse_file_type(f):
            if not re.search(FILTER, f):
                raise ValueError(f"{f} is not a valid file filter")
    for f in filters:
        parse_file_type(f)


# --- HTTP server ----------------------------------------------------------------------------------


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_theme_setting_and_title_bar(api, tmp_path, monkeypatch):
    """The theme is saved, a bad value is refused, and the page's choice reaches the title bar."""
    import main

    monkeypatch.setenv("AURORA_DATA_DIR", str(tmp_path))
    assert ok(call(api, "settings_set", theme="light"))["settings"]["theme"] == "light"
    assert call(api, "settings_set", theme="purple")["error"] == "INVALID"
    assert ok(call(api, "state"))["settings"]["theme"] == "light"
    assert main.start_dark() is False  # the first frame already has the saved colours
    ok(call(api, "settings_set", theme="dark"))
    assert main.start_dark() is True

    ok(call(api, "window_theme", dark=False))  # no window (tests, browser mode): nothing to do
    seen = []
    api._titlebar = seen.append
    ok(call(api, "window_theme", dark=False))
    ok(call(api, "window_theme", dark=True))
    assert seen == [False, True]


def test_http_server(tmp_path):
    session = Session()
    api = Api(session, Dialogs(), test_mode=False)
    srv = AppServer(api, session, WEB_DIR).start()
    try:
        base = srv.url
        code, body, hdr = http(base)
        assert code == 200 and b"Aurora" in body and "text/html" in hdr["Content-Type"]
        assert http(f"http://127.0.0.1:{srv.port}/wrongtoken/")[0] == 404
        assert http(base + "../core/session.py")[0] == 404
        # API: JSON only, same origin only
        code, body, _ = http(base + "api/state", b"{}", {"Content-Type": "application/json"})
        assert code == 200 and json.loads(body)["doc"] == {"open": False}
        assert http(base + "api/state", b"{}", {"Content-Type": "text/plain"})[0] == 415
        assert http(base + "api/state", b"{}", {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
        assert json.loads(http(base + "api/test_dialogs", b"{}", {"Content-Type": "application/json"})[1])["error"] == "NOT_FOUND"
        path = save_tmp(make_doc(2), tmp_path, "h.pdf")
        code, body, _ = http(base + "api/open_path", json.dumps({"path": path}).encode(), {"Content-Type": "application/json"})
        rev = json.loads(body)["doc"]["pages"][1][2]
        code, body, hdr = http(base + f"page/{rev}/600.jpg")
        assert code == 200 and body[:2] == b"\xff\xd8" and "immutable" in hdr["Cache-Control"]
        code, body, _ = http(base + f"info/{rev}.json")
        info = json.loads(body)
        assert code == 200 and any(w[5] == "marker-2" for w in info["words"])
        assert http(base + "page/99999999/600.jpg")[0] == 404
        code, body, _ = http(base + f"print?revs={rev}&w=800")
        assert code == 200 and f"page/{rev}/800.jpg".encode() in body
        # a request for another host name is refused (DNS rebinding)
        req = urllib.request.Request(base, headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)
    finally:
        srv.stop()
        session.close()


def test_selftest_passes(tmp_path, monkeypatch):
    """The installation check used by install.bat runs clean on a working setup."""
    from core import selftest

    monkeypatch.setenv("AURORA_DATA_DIR", str(tmp_path))
    assert selftest.run() == 0
    report = (tmp_path / "selftest.txt").read_text(encoding="utf-8")
    assert "All checks passed." in report and "FAIL" not in report
