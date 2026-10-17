"""Backend API and HTTP server, driven the way the interface drives them.

Native dialogs are replaced by a scripted queue (Dialogs.script), everything else is the
production code path: Api.dispatch -> Session -> engine, and real HTTP for the server.
"""
import base64
import hashlib
import json
import os
import re
import shutil
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
    a._jobs.stop_all()  # as main() does: a job left running must not work on a closed document
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


def test_failed_edit_rolls_back(api, tmp_path, monkeypatch):
    res, _ = opened(api, tmp_path)
    revs = [p[2] for p in res["doc"]["pages"]]
    bad = call(api, "reorder", order=[0, 0, 1])  # not a permutation
    assert not bad["ok"]
    real = pt.rotate_pages

    def half(doc, pages, degrees):  # fails after it changed the document
        real(doc, pages, degrees)
        raise RuntimeError("disk full")

    monkeypatch.setattr(pt, "rotate_pages", half)
    assert call(api, "rotate", pages=[0], degrees=90)["error"] == "ERROR"
    d = call(api, "state")["doc"]
    assert [p[2] for p in d["pages"]] == revs and not d["dirty"] and d["undo"] == ""
    assert api.s.doc[0].rotation == 0


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
    before = [p[2] for p in d["pages"]]
    d = ok(call(api, "insert_file_dialog", paths=[save_tmp(make_doc(1), tmp_path, "one.pdf")], at=1))["doc"]
    after = [p[2] for p in d["pages"]]
    assert after[:1] + after[2:] == before and after[1] not in before  # the pages after it moved, unchanged
    assert ok(call(api, "delete_pages"))["doc"]["undo"] == "insert"  # no pages named: no step


def test_a_damaged_picture_is_not_supported(api, tmp_path):
    opened(api, tmp_path)
    bad = tmp_path / "scan.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"cut off" * 50)
    assert call(api, "insert_file_dialog", paths=[str(bad)])["error"] == "NOT_SUPPORTED"
    assert call(api, "open_path", path=str(bad))["error"] == "NOT_SUPPORTED"


def test_a_pure_xfa_form_is_told_apart_from_a_hybrid_one(api, tmp_path):
    """MuPDF does not draw XFA: a dynamic XFA form shows only its "please wait" page, so the UI says why."""
    def with_xfa(doc, name, needs_rendering):
        cat = doc.pdf_catalog()
        x = doc.get_new_xref()
        doc.update_object(x, "<<>>")
        doc.update_stream(x, b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
        doc.xref_set_key(cat, "AcroForm/XFA", f"{x} 0 R")
        if needs_rendering:
            doc.xref_set_key(cat, "NeedsRendering", "true")
        return save_tmp(doc, tmp_path, name)

    hybrid = make_doc(1)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_TEXT, "navn", pymupdf.Rect(50, 50, 200, 70)
    hybrid[0].add_widget(w)
    for path, xfa in ((with_xfa(make_doc(1), "dynamic.pdf", True), True),
                      (with_xfa(make_doc(1), "no-fields.pdf", False), True),
                      (with_xfa(hybrid, "hybrid.pdf", False), False),
                      (save_tmp(make_doc(1), tmp_path, "plain.pdf"), False)):
        assert ok(call(api, "open_path", path=path))["doc"]["xfa"] is xfa, path


def test_a_page_that_loses_a_link_is_drawn_again(api, tmp_path):
    """Deleting a page takes away the links to it from the other pages: they change too."""
    doc = make_doc(3)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(10, 10, 50, 50), "page": 2})
    revs = [p[2] for p in ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "links.pdf")))["doc"]["pages"]]
    pages = ok(call(api, "delete_pages", pages=[2]))["doc"]["pages"]
    assert pages[0][2] != revs[0] and pages[1][2] == revs[1]


def test_save_keeps_a_version_another_program_wrote_meanwhile(api, tmp_path):
    """A synced or a colleague's newer version in the file's place is not overwritten: Save As is offered."""
    _, path = opened(api, tmp_path)
    ok(call(api, "rotate", pages=[0]))
    save_tmp(make_doc(1), tmp_path, "doc.pdf")
    newer = open(path, "rb").read()
    assert call(api, "save")["error"] == "FILE_CHANGED" and open(path, "rb").read() == newer
    answers(api, str(tmp_path / "mine.pdf"))
    ok(call(api, "save_as"))
    ok(call(api, "rotate", pages=[0]))
    assert ok(call(api, "save"))["doc"]["name"] == "mine.pdf"  # its own file again: saved as usual


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


def test_unprotected_save_over_the_open_file_updates_the_session(api, tmp_path, monkeypatch):
    """Picking the open file itself for "save without protection" makes it the unprotected version;
    the session used to keep the old protection and then asked for passwords on the next save."""
    locked = str(tmp_path / "l.pdf")
    pt.save_pdf(make_doc(2), locked, user_pw="u1", owner_pw="o1")
    ok(call(api, "open_path", path=locked, password="o1"))  # the owner; the open password stays unknown

    def in_use(*args, **kwargs):
        raise PermissionError("the file is open in another program")

    with monkeypatch.context() as m:  # a write that fails changes nothing, in the file or in the app
        m.setattr(pt, "save_pdf", in_use)
        answers(api, locked)
        assert call(api, "save_unprotected")["error"] == "FILE_LOCKED"
    d = call(api, "state")["doc"]
    assert d["protected"] and not d["dirty"] and pymupdf.open(locked).needs_pass
    answers(api, locked)
    d = ok(call(api, "save_unprotected"))["doc"]
    assert not d["protected"] and not d["dirty"] and not pymupdf.open(locked).needs_pass
    ok(call(api, "rotate", pages=[0], degrees=90))
    ok(call(api, "save"))
    doc = pymupdf.open(locked)
    assert not doc.needs_pass and doc[0].rotation == 90


def test_extracted_and_split_pages_keep_the_protection(api, tmp_path):
    locked = str(tmp_path / "l.pdf")
    pt.save_pdf(make_doc(3), locked, user_pw="u1", owner_pw="o1", allowed=["print", "assemble"])
    ok(call(api, "open_path", path=locked, password="u1"))
    out = str(tmp_path / "pages.pdf")
    answers(api, out)
    ok(call(api, "extract_dialog", pages=[1]))
    _, info = pt.load_for_editing(out, "u1")
    assert info["protected"] and not info["owner"] and not pt.rights_for(info)["copy"]
    with pytest.raises(ValueError, match="NEED_PASSWORD"):
        pt.load_for_editing(out)
    folder = tmp_path / "parts"
    folder.mkdir()
    answers(api, str(folder))
    ok(call(api, "split_dialog", every=2))
    assert sorted(pymupdf.open(str(p)).needs_pass for p in folder.iterdir()) == [True, True]
    for kind in ("txt", "docx", "images", "embedded"):  # these take the content out: copying is not allowed here
        assert call(api, "export_start", kind=kind)["error"] == "NOT_ALLOWED"
    # the owner without the open password is asked for it before picking a file, as when saving
    ok(call(api, "open_path", path=locked, password="o1"))
    answers(api, str(tmp_path / "never.pdf"))
    assert call(api, "extract_dialog", pages=[0])["error"] == "NEED_PASSWORDS"
    assert api._dialogs.script == [str(tmp_path / "never.pdf")]
    # ... or takes the copy without protection: the open document keeps its own and has nothing to save
    plain = str(tmp_path / "plain.pdf")
    answers(api, plain)
    ok(call(api, "extract_dialog", pages=[0], unprotected=True))
    d = call(api, "state")["doc"]
    assert not pymupdf.open(plain).needs_pass and d["protected"] and not d["dirty"]
    ok(call(api, "set_protection", user_pw="u1"))  # the open password, told later, is no change either
    assert not call(api, "state")["doc"]["dirty"]
    ok(call(api, "open_path", path=locked, password="u1"))  # not the owner: no copy without protection
    answers(api, str(tmp_path / "never.pdf"))
    assert call(api, "extract_dialog", pages=[0], unprotected=True)["error"] == "NOT_ALLOWED"
    assert api._dialogs.script == [str(tmp_path / "never.pdf")]


def test_merge_and_insert_take_a_protected_file_with_its_owner_password(api, tmp_path):
    """The result does not carry that file's protection, so only its owner may do this (the rule for
    saving without protection). The answer names the file, and the interface asks for the password."""
    theirs = str(tmp_path / "theirs.pdf")
    pt.save_pdf(make_doc(2), theirs, owner_pw="boss", allowed=["print"])  # opens without a password
    locked = str(tmp_path / "locked.pdf")
    pt.save_pdf(make_doc(1), locked, user_pw="pw", owner_pw="boss", allowed=["print"])
    mine = save_tmp(make_doc(1), tmp_path, "mine.pdf")
    out = tmp_path / "merged.pdf"
    answers(api, str(out))
    files = [mine, theirs, locked]
    r = call(api, "merge_run", paths=files)
    assert (r["error"], r["path"], r["name"]) == ("SOURCE_PROTECTED", theirs, "theirs.pdf")
    assert api._dialogs.script == [str(out)] and not out.exists()  # asked before where the result goes
    r = call(api, "merge_run", paths=files, passwords={theirs: "boss", locked: "pw"})
    assert (r["error"], r["path"]) == ("SOURCE_PROTECTED", locked)  # the open password is not enough
    r = call(api, "merge_run", paths=files, passwords={theirs: "boss", locked: "nope"})
    assert (r["error"], r["path"]) == ("WRONG_PASSWORD", locked)
    assert ok(call(api, "merge_run", paths=files, passwords={theirs: "boss", locked: "boss"}))["pages"] == 4
    assert not pymupdf.open(str(out)).is_encrypted

    res, _ = opened(api, tmp_path, pages=2)
    revs = [p[2] for p in res["doc"]["pages"]]
    answers(api, theirs)  # picked in the file dialog
    r = call(api, "insert_file_dialog", at=1)
    assert (r["error"], r["path"]) == ("SOURCE_PROTECTED", theirs)
    assert call(api, "insert_file_dialog", at=1, paths=[locked])["error"] == "SOURCE_PROTECTED"
    assert call(api, "insert_file_dialog", at=1, paths=[locked], password="nope")["error"] == "WRONG_PASSWORD"
    d = call(api, "state")["doc"]
    assert [p[2] for p in d["pages"]] == revs and not d["dirty"]
    assert ok(call(api, "insert_file_dialog", at=1, paths=[theirs], password="boss"))["count"] == 2
    renamed = str(tmp_path / "theirs.dat")  # a PDF by what it holds, whatever it is called
    shutil.copy(theirs, renamed)
    r = call(api, "insert_file_dialog", at=0, paths=[renamed])
    assert (r["error"], r["path"]) == ("SOURCE_PROTECTED", renamed)
    answers(api, [renamed])
    assert call(api, "insert_images_dialog", at=0)["error"] == "SOURCE_PROTECTED"
    pics = tmp_path / "pics.pdf"
    answers(api, [renamed], str(pics))
    assert call(api, "images_to_pdf_dialog")["error"] == "SOURCE_PROTECTED" and not pics.exists()
    free = str(tmp_path / "free.pdf")
    pt.save_pdf(make_doc(1), free, owner_pw="boss")  # encrypted, but every permission granted: nothing to lose
    answers(api, str(out))
    assert ok(call(api, "merge_run", paths=[mine, free]))["pages"] == 2  # as do unprotected files


def test_copies_never_replace_the_open_file(api, tmp_path):
    """Compressing, extracting or merging into the open file would leave the app showing a document
    the file no longer holds (and a signed original would lose its signature)."""
    _, path = opened(api, tmp_path)
    before = open(path, "rb").read()
    answers(api, path)
    assert call(api, "export_start", kind="compress")["error"] == "OPEN_FILE"
    answers(api, path)
    assert call(api, "extract_dialog", pages=[0])["error"] == "OPEN_FILE"
    other = save_tmp(make_doc(1), tmp_path, "other.pdf")
    answers(api, path)
    assert call(api, "merge_run", paths=[other, other])["error"] == "OPEN_FILE"
    assert open(path, "rb").read() == before


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


def test_open_draws_the_fields_of_a_form_that_asks_for_it(api, tmp_path):
    """NeedAppearances: the fields are drawn once on opening, which is not a change to save."""
    doc = make_doc(1)
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.rect = "mesto", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(50, 400, 250, 425)
    w.field_value = "Stari"
    doc[0].add_widget(w)
    doc.xref_set_key(doc[0].first_widget.xref, "V", pymupdf.get_pdf_str("Đurđevac"))  # the old drawing stays
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/NeedAppearances", "true")
    d = ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "need.pdf")))["doc"]
    assert not d["dirty"] and d["undo"] == ""
    ok(call(api, "flatten"))
    text = call(api, "page_text", page=0)["text"]
    assert "Đurđevac" in text and "Stari" not in text


def test_links_follow_their_page_when_pages_move(api, tmp_path):
    """A page keeps its revision (and cached overlay) when pages are inserted before its link
    target, so the link names the target page object, not its position."""
    doc = make_doc(3)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(50, 60, 200, 85), "page": 2})
    path = save_tmp(doc, tmp_path, "links.pdf")
    rev0 = ok(call(api, "open_path", path=path))["doc"]["pages"][0][2]
    link = json.loads(api_server_info(api, rev0))["links"][0]

    def target(d):
        return [p[3] for p in d["pages"]].index(link["px"])

    assert target(call(api, "state")["doc"]) == 2
    d = ok(call(api, "insert_blank", at=1))["doc"]
    assert d["pages"][0][2] == rev0 and target(d) == 3  # same overlay, the target moved on
    d = ok(call(api, "reorder", order=[3, 0, 1, 2]))["doc"]
    assert target(d) == 0
    d = ok(call(api, "undo"))["doc"]
    assert target(d) == 3


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


def test_settings_that_cannot_be_written_do_not_fail_the_work(api, tmp_path, monkeypatch):
    """settings.json held by another program (backup, antivirus): the recent list goes, the open and save stay."""
    from core import settings

    monkeypatch.setattr(settings, "_file", lambda: tmp_path / "missing" / "settings.json")
    ok(call(api, "open_path", path=save_tmp(make_doc(1), tmp_path, "a.pdf")))
    ok(call(api, "rotate", pages=[0]))
    assert ok(call(api, "save"))["doc"]["name"] == "a.pdf"


def test_the_open_file_is_merged_only_as_saved_on_screen(api, tmp_path):
    """Merging reads files from disk: the open file's unsaved changes would be missing from the result."""
    _, path = opened(api, tmp_path)
    other = save_tmp(make_doc(1), tmp_path, "other.pdf")
    ok(call(api, "delete_pages", pages=[0]))
    answers(api, str(tmp_path / "m.pdf"))
    assert call(api, "merge_run", paths=[path, other])["error"] == "SAVE_FIRST"
    ok(call(api, "save"))
    assert ok(call(api, "merge_run", paths=[path, other]))["pages"] == 3


def test_a_field_on_two_pages_is_redrawn_on_both(api, tmp_path):
    """A page gets a new revision when it changes; the page image of a field's other widget changes too."""
    from test_engine import _form_made_elsewhere

    doc, _, x = _form_made_elsewhere()  # "reka" has a widget on page 1 and on page 2
    revs = [p[2] for p in ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "form.pdf")))["doc"]["pages"]]
    pages = ok(call(api, "field_set", page=0, xref=x["reka"], value="Ниш"))["doc"]["pages"]
    assert [p[2] != r for p, r in zip(pages, revs)] == [True, True]


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


def test_word_export_leaves_the_document_free(api, tmp_path):
    """pdf2docx can take minutes. It runs in a process of its own, so pages keep loading meanwhile
    (PyMuPDF must not run on two threads of one process at once, hence not just a thread)."""
    pytest.importorskip("pdf2docx")
    opened(api, tmp_path, pages=30)
    out = str(tmp_path / "long.docx")
    answers(api, out)
    job = ok(call(api, "export_start", kind="docx"))["job"]
    free = 0
    while call(api, "job_status", id=job)["job"]["state"] == "running":
        if api.s.lock.acquire(timeout=0.5):
            api.s.lock.release()
            free += 1
        time.sleep(0.05)
    st = call(api, "job_status", id=job)["job"]
    assert st["state"] == "done" and os.path.getsize(out) > 1000, st
    assert free >= 3


def test_closing_stops_a_running_export_which_cleans_up(api, tmp_path, monkeypatch):
    """The process ends when the window closes: a running job is cancelled and waited for first, so the Word
    export's plain copy of the document does not stay behind in the temp folder."""
    opened(api, tmp_path)
    copies = []

    def slow(pdf, out, progress=None, password=None):
        copies.append(pdf)
        while progress(0, 1) is not False:
            time.sleep(0.02)
        raise pt.Cancelled()

    monkeypatch.setattr(pt, "export_docx", slow)
    answers(api, str(tmp_path / "out.docx"))
    job = ok(call(api, "export_start", kind="docx"))["job"]
    for _ in range(500):
        if copies:
            break
        time.sleep(0.01)
    assert os.path.exists(copies[0]) and call(api, "state")["jobs"]
    api._jobs.stop_all(timeout=5)
    assert call(api, "job_status", id=job)["job"]["state"] == "cancelled" and not os.path.exists(copies[0])


def test_undo_keeps_to_its_limits(api, tmp_path, monkeypatch):
    from core import session as sess

    monkeypatch.setattr(sess, "MAX_UNDO", 2)
    opened(api, tmp_path)
    for _ in range(4):
        ok(call(api, "rotate", pages=[0], degrees=90))
    assert len(api.s.undo_stack) == 2
    monkeypatch.setattr(sess, "MAX_UNDO_BYTES", 1)  # past the memory limit the last step still stays
    ok(call(api, "rotate", pages=[0], degrees=90))
    assert len(api.s.undo_stack) == 1


def test_big_undo_steps_wait_on_disk(api, tmp_path, monkeypatch):
    from core import session as sess

    monkeypatch.setattr(sess, "SPILL_BYTES", 1)  # every snapshot counts as big here
    monkeypatch.setattr(sess, "_CHUNK", 1000)  # and goes in parts (AES-GCM takes at most 2 GiB at once)
    opened(api, tmp_path)
    ok(call(api, "rotate", pages=[0], degrees=90))
    ok(call(api, "rotate", pages=[0], degrees=90))
    assert [s.on_disk for s in api.s.undo_stack] == [True, True]
    snap = api.s.undo_stack[0]
    snap._file.seek(0)
    spilled = snap._file.read()
    assert b"%PDF" not in spilled  # encrypted: a protected document is not readable in the temp folder
    assert len(spilled) == snap.size + 16 * -(-snap.size // 1000) and snap.size > 3000  # a tag per part
    ok(call(api, "undo"))
    ok(call(api, "undo"))
    assert api.s.doc[0].rotation == 0 and not call(api, "state")["doc"]["dirty"]
    ok(call(api, "redo"))
    assert api.s.doc[0].rotation == 90
    cut = sess.Snapshot("cut", os.urandom(2500), [], [], 0, False)  # three parts; the temp file loses the last
    cut._file.truncate(2 * 1016)
    with pytest.raises(OSError):
        cut.data  # noqa: B018 - a short undo step is refused, not shown


def test_a_link_on_a_turned_page_is_where_it_shows(api, tmp_path):
    """MuPDF gives a page's links in display space; turning them again put every link of a turned page elsewhere."""
    from test_engine import page_ctm

    doc = make_doc(1, rotation=90)
    link = doc.get_new_xref()
    doc.update_object(link, "<</Type/Annot/Subtype/Link/Rect[220 300 300 330]/A<</S/URI/URI(https://example.com)>>>>")
    doc.xref_set_key(doc[0].xref, "Annots", f"[{link} 0 R]")
    path = save_tmp(doc, tmp_path, "turned.pdf")
    for step in (None, 90):  # as opened, and turned once more in the app
        d = ok(call(api, "rotate", pages=[0], degrees=step))["doc"] if step else ok(call(api, "open_path", path=path))["doc"]
        got = json.loads(api_server_info(api, d["pages"][0][2]))["links"][0]["r"]
        want = pymupdf.Rect(220, 300, 300, 330) * page_ctm(api.s.doc[0])
        assert max(abs(a - b) for a, b in zip(got, want)) < 1, (step, got, want)


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



@pytest.mark.parametrize("algorithm", ("aes256", "aes128"))
def test_a_file_protected_here_can_be_signed(api, tmp_path, algorithm):
    """Protect the open file, then sign it: the permissions set here lacked the bits the PDF rules keep set,
    pyHanko then wanted a MAC and the signing failed. The signed copy keeps the kind of protection."""
    _, src = opened(api, tmp_path)
    answers(api, src)  # the protect dialog picks the open file itself
    ok(call(api, "save_protected", user_pw="tajna", owner_pw="gazda", allowed=["print", "form"], algorithm=algorithm))
    ident = ok(call(api, "id_create", name="Marko Test", password="pw1234"))["id"]
    out = str(tmp_path / "signed.pdf")
    answers(api, out)
    ok(call(api, "sign_digital", id=ident, password="pw1234", page=0, rect=[200, 300, 380, 360]))
    d = pymupdf.open(out)
    assert pt._algorithm(pt._encrypt_dict(d)) == algorithm and d.authenticate("tajna")
    assert _statuses(out, "tajna") == ["valid"]


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


def test_an_empty_signature_field_is_no_signature(api, tmp_path):
    doc = make_doc(1)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_SIGNATURE, "sig", pymupdf.Rect(50, 50, 200, 90)
    doc[0].add_widget(w)
    assert not ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "empty.pdf")))["doc"]["signed"]
    assert ok(call(api, "open_path", path=_signed_file(tmp_path, "signed.pdf")))["doc"]["signed"]


def test_a_signed_file_replaced_on_disk_is_copied_and_signed_as_opened(api, tmp_path):
    """Save As copies a signed, unchanged file byte for byte, and a further signature goes on top of its bytes:
    the bytes as opened, not what another program has put in the file's place since."""
    f = _signed_file(tmp_path, "contract.pdf")
    as_opened = open(f, "rb").read()
    ok(call(api, "open_path", path=f))
    save_tmp(make_doc(1), tmp_path, "contract.pdf")  # a sync tool puts another version there
    assert [i["status"] for i in ok(call(api, "signatures_validate"))["items"]] == ["valid"]
    copy = str(tmp_path / "copy.pdf")
    answers(api, copy)
    ok(call(api, "save_as"))
    assert open(copy, "rb").read() == as_opened
    save_tmp(make_doc(1), tmp_path, "copy.pdf")
    second = ok(call(api, "id_create", name="Bo Andre", password="pw5678"))["id"]
    out = str(tmp_path / "countersigned.pdf")
    answers(api, out)
    ok(call(api, "sign_digital", id=second, password="pw5678"))
    assert open(out, "rb").read().startswith(as_opened) and _statuses(out) == ["valid", "valid"]


def test_signing_needs_the_right_to_fill_in_and_a_file_of_its_own(api, tmp_path):
    """A file whose owner allows printing only is not signed with its open password, one that allows filling in
    is signed only in its own empty signature field (a new one changes the form), and the signed copy never
    takes the place of the open file, which would lose its own signature."""
    ident = ok(call(api, "id_create", name="Bo Andre", password="pw5678"))["id"]
    doc = make_doc(1)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = pymupdf.PDF_WIDGET_TYPE_SIGNATURE, "potpis", pymupdf.Rect(50, 400, 250, 450)
    doc[0].add_widget(w)
    for allowed, field, error in ((["print"], None, "NOT_ALLOWED"), (["print"], "potpis", "NOT_ALLOWED"),
                                  (["print", "form"], None, "NOT_ALLOWED"), (["print", "form"], "potpis", None)):
        path = str(tmp_path / "policy.pdf")
        pt.save_pdf(doc, path, user_pw="open1", owner_pw="owner1", allowed=allowed)
        ok(call(api, "open_path", path=path, password="open1"))
        answers(api, str(tmp_path / f"signed-{len(allowed)}-{field}.pdf"))
        assert call(api, "sign_digital", id=ident, password="pw5678", field=field).get("error") == error, (allowed, field)
    f = _signed_file(tmp_path, "contract.pdf")
    ok(call(api, "open_path", path=f))
    answers(api, f)
    assert call(api, "sign_digital", id=ident, password="pw5678")["error"] == "OPEN_FILE"
    assert _statuses(f) == ["valid"]


def test_keep_protection_asks_only_for_the_open_password(api, tmp_path):
    path = str(tmp_path / "policy.pdf")
    pt.save_pdf(make_doc(2), path, user_pw="open1", owner_pw="owner1", allowed=["print"])
    ok(call(api, "open_path", path=path, password="owner1"))  # the owner opens it
    ok(call(api, "rotate", pages=[0]))
    assert call(api, "save")["error"] == "NEED_PASSWORDS"
    answers(api, str(tmp_path / "copy.pdf"))
    assert call(api, "save_as")["error"] == "NEED_PASSWORDS"  # asked before a file is picked, not after
    assert api._dialogs.script == [str(tmp_path / "copy.pdf")]
    answers(api, None)  # "save the copy without protection", then the file dialog is cancelled: nothing changes
    assert call(api, "save_as", unprotected=True)["error"] == "CANCELLED"
    assert call(api, "state")["doc"]["protected"]
    assert call(api, "set_protection", user_pw="typo")["error"] == "WRONG_PASSWORD"
    ok(call(api, "set_protection", user_pw="open1"))
    ok(call(api, "save"))
    _, info = pt.load_for_editing(path, "open1")
    assert not info["owner"] and pt.rights_for(info)["print"] and not pt.rights_for(info)["modify"]
    assert pt.load_for_editing(path, "owner1")[1]["owner"]
    plain = str(tmp_path / "plain.pdf")
    answers(api, plain)  # a copy without protection, written: the app goes on with that file
    d = ok(call(api, "save_as", unprotected=True))["doc"]
    assert not d["protected"] and d["path"] == os.path.abspath(plain) and not pymupdf.open(plain).needs_pass
    assert pymupdf.open(path).needs_pass


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


def test_a_redaction_that_would_leave_text_under_a_mark_changes_nothing(api, tmp_path, monkeypatch):
    """Text drawn through a pattern that stays under a mark (here: a pattern copy that kept its text) stops the
    redaction with a message the interface shows, and the document stays as it was, marks and all."""
    from core import annots as an

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    pat, c = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(pat, "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 612 792]/XStep 612"
                           "/YStep 792/Resources<</Font<</F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>")
    doc.update_stream(pat, b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET")
    doc.update_object(c, "<<>>")
    doc.update_stream(c, b"q /Pattern cs /P0 scn 0 0 612 792 re f Q")
    doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<</Pattern<</P0 {pat} 0 R>>>>")
    ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "patterned.pdf")))
    ok(call(api, "annot_add", page=0, kind="redact", rects=[[60, 75, 200, 100]]))
    monkeypatch.setattr(an, "_without_text", lambda doc, xref, *_: xref)
    assert call(api, "redact_apply")["error"] == "REDACT_LEFT"
    assert call(api, "redact_count")["count"] == 1 and "SECRET1" in call(api, "page_text", page=0)["text"]
    monkeypatch.undo()
    assert ok(call(api, "redact_apply"))["count"] == 1
    assert "SECRET1" not in call(api, "page_text", page=0)["text"]


def test_a_redaction_that_would_move_text_changes_nothing(api, tmp_path, monkeypatch):
    """Should MuPDF's rewrite of the page still move text away from the marks (here: without our help against it),
    the redaction stops with a message the interface shows, and the document stays as it was, marks and all."""
    from core import annots as an

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    c = doc.get_new_xref()
    doc.update_object(c, "<<>>")
    doc.update_stream(c, b"BT /F9 12 Tf 72 700 Td (SECRET1) Tj ET "
                         b"BT /F9 12 Tf 72 500 Td (KEEPME) Tj 0 0 Td (KEEPME) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
    doc.xref_set_key(page.xref, "Resources", "<</Font<</F9<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")
    ok(call(api, "open_path", path=save_tmp(doc, tmp_path, "overprint.pdf")))
    ok(call(api, "annot_add", page=0, kind="redact", rects=[[60, 75, 200, 100]]))
    monkeypatch.setattr(an, "_steady", lambda data, *args, **kw: data)
    assert call(api, "redact_apply")["error"] == "REDACT_MOVED"
    assert call(api, "redact_count")["count"] == 1 and "SECRET1" in call(api, "page_text", page=0)["text"]
    monkeypatch.undo()
    assert ok(call(api, "redact_apply"))["count"] == 1
    assert "SECRET1" not in call(api, "page_text", page=0)["text"]


def test_a_signature_on_a_page_with_a_user_unit_looks_as_on_other_pages(api, tmp_path):
    """On a page with /UserUnit 2 a unit of the file is two points on screen: the text of a visible signature
    came out twice as big as on other pages."""
    ident = ok(call(api, "id_create", name="Marko Test", password="pw1234"))["id"]
    rows = []
    for unit in (1, 2):
        doc = pymupdf.open()
        doc.new_page(width=612 / unit, height=792 / unit)
        doc.xref_set_key(doc.page_xref(0), "UserUnit", str(unit))
        ok(call(api, "open_path", path=save_tmp(doc, tmp_path, f"unit{unit}.pdf")))
        out = str(tmp_path / f"signed{unit}.pdf")
        answers(api, out)
        ok(call(api, "sign_digital", id=ident, password="pw1234", page=0, rect=[150, 300, 450, 380]))
        pix = pymupdf.open(out)[0].get_pixmap(dpi=72, alpha=False)
        rows.append([y for y in range(300, 380) if any(sum(pix.pixel(x, y)) < 450 for x in range(150, 450))])
    assert rows[0] and abs(len(rows[1]) - len(rows[0])) <= 3 and abs(rows[1][0] - rows[0][0]) <= 3, rows


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


LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a system proxy would get 127.0.0.1 too


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with LOCAL.open(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_the_install_check_reaches_the_app_past_a_proxy(monkeypatch):
    """1_INSTALLER.bat ends with this check; a proxy in the Windows settings got 127.0.0.1 too and failed it."""
    from core import selftest

    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing answers there
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert "127.0.0.1" in selftest.check_server()


@pytest.mark.parametrize("stuck", (False, True))
def test_closing_never_takes_the_document_from_a_job(api, tmp_path, stuck):
    """The window closes while OCR reads a page. The job stops after that page, then the document closes; a job
    still on its page after the wait keeps the document, which it would otherwise find closed after that page."""
    import threading
    import types

    import main

    opened(api, tmp_path)
    reading, go_on = threading.Event(), threading.Event()

    def one(page):  # one page of OCR, which takes its time
        reading.set()
        go_on.wait(10)
        return False

    job = api._jobs.start("ocr", lambda job: api.s.run_pages("ocr", [0, 1, 2], one, progress=job.progress))
    assert reading.wait(5)
    if not stuck:
        threading.Timer(0.2, go_on.set).start()
    stopped = []
    main.shutdown(api, api.s, types.SimpleNamespace(stop=lambda: stopped.append(True)), wait=1.0)
    assert stopped and (api.s.doc is not None) == stuck
    go_on.set()
    api._jobs.stop_all()
    assert api._jobs.status(job)["state"] == "cancelled"  # after its page, not in an error on a closed document


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


def test_settings_changed_from_two_threads_keep_both_changes(tmp_path, monkeypatch):
    """Requests run on several threads; each change used to read the file, edit and write it
    back unguarded, so two at once could undo each other."""
    import threading

    from core import settings

    monkeypatch.setenv("AURORA_DATA_DIR", str(tmp_path))
    read = settings.load
    both_read = threading.Barrier(2)

    def slow_load():
        data = read()
        try:
            both_read.wait(timeout=0.5)  # both threads hold the old settings before either writes
        except threading.BrokenBarrierError:
            pass
        return data

    monkeypatch.setattr(settings, "load", slow_load)
    doc = str(tmp_path / "a.pdf")
    threads = [threading.Thread(target=settings.update, kwargs={"author": "Ana"}),
               threading.Thread(target=settings.add_recent, args=(doc,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = read()
    assert final["author"] == "Ana" and final["recent"][0]["path"] == doc


def test_shortcuts_and_pdf_handler_carry_the_window_app_id():
    """main.py gives the window an explicit taskbar ID. Unless the shortcuts and the PDF handler carry
    the same ID, Windows pins pythonw.exe itself, which shows Python's icon and opens nothing."""
    import main

    script = (WEB_DIR.parent / "scripts" / "windows.ps1").read_text(encoding="utf-8-sig")
    assert f"$appId = '{main.APP_ID}'" in script
    assert "[Aurora.Link]::SetAppId($lnk, $appId)" in script
    assert "$k.SetValue('AppUserModelID', $appId)" in script


def test_a_page_that_cannot_be_drawn_is_logged(api, tmp_path, monkeypatch, caplog):
    """Under pythonw the log is the only trace of a failed request; an aborted image load stays out of it."""
    opened(api, tmp_path)

    def broken(page, width):
        raise RuntimeError("cannot draw this page")

    monkeypatch.setattr(pt, "render_pixmap", broken)
    srv = AppServer(api, api.s, WEB_DIR).start()
    try:
        http(srv.url + f"page/{api.s.revs[0]}/800.jpg")
    except Exception:  # the connection closes without an answer
        pass
    finally:
        srv.stop()
    assert any("cannot draw this page" in r.getMessage() + str(r.exc_info) and r.levelname == "ERROR"
               for r in caplog.records)


def test_http_server(tmp_path):
    session = Session()
    api = Api(session, Dialogs(), test_mode=False)
    srv = AppServer(api, session, WEB_DIR).start()
    try:
        base = srv.url
        code, body, hdr = http(base)
        assert code == 200 and b"Aurora" in body and "text/html" in hdr["Content-Type"]
        # markup slipped into the interface (a file name, a comment) runs no script, sends nothing elsewhere
        csp = hdr["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
        assert hdr["Referrer-Policy"] == "no-referrer"  # every address holds the secret token
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
        code, body, hdr = http(base + f"print?revs={rev}&w=800")
        assert code == 200 and f"page/{rev}/800.jpg".encode() in body
        for tag in ("script", "style"):  # the print page's own script and style, named in its policy by their hash
            inline = re.search(rf"<{tag}>(.*?)</{tag}>".encode(), body, re.S).group(1)
            digest = base64.b64encode(hashlib.sha256(inline).digest()).decode()
            assert f"'sha256-{digest}'" in hdr["Content-Security-Policy"]
        # a request for another host name is refused (DNS rebinding)
        req = urllib.request.Request(base, headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError):
            LOCAL.open(req, timeout=5)
    finally:
        srv.stop()
        session.close()


def test_selftest_passes(tmp_path, monkeypatch):
    """The installation check 1_INSTALLER.bat ends with runs clean on a working setup. It also tells when the image
    pass of compression could not run in its process: compression then goes on without it, and the copy still
    comes out smaller."""
    from core import pdftools, selftest

    monkeypatch.setenv("AURORA_DATA_DIR", str(tmp_path))
    assert selftest.run() == 0
    report = (tmp_path / "selftest.txt").read_text(encoding="utf-8")
    assert "All checks passed." in report and "FAIL" not in report

    def no_process(*args):
        raise RuntimeError("the image pass stopped (exit code 1)")

    monkeypatch.setattr(pdftools, "_in_process", no_process)
    with pytest.raises(RuntimeError, match="image pass did not run"):
        selftest.check_compress(tmp_path)
