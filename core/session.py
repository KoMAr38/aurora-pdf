"""The open document and everything that changes it.

One PyMuPDF document lives here behind a re-entrant lock: MuPDF is not thread-safe and
both the HTTP server (page images) and the API (edits) run on worker threads, so every
PyMuPDF call goes through ``session.lock``.

Each page carries a revision number. A revision identifies one exact page content, so the
viewer can cache page images and overlay data under it forever; any change to a page gives
it a fresh revision. Undo/redo stores whole-document snapshots together with their
revisions, which makes undo instant on screen (the old images are still cached).
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import os
import secrets
import tempfile
import threading
import time

import pymupdf

from . import annots as an
from . import ocr
from . import pdftools as pt
from .paths import write_atomic

_REV = itertools.count(1)
_STATE = itertools.count(1)
_DOC_ID = itertools.count(1)
MAX_UNDO = 60
MAX_UNDO_BYTES = 700 * 1024 * 1024
SPILL_BYTES = 2 * 1024 * 1024  # bigger undo steps wait in a temporary file, not in memory
_CHUNK = 16 * 1024 * 1024  # a spilled step is encrypted in parts: AES-GCM takes at most 2 GiB at once


def _stamp(path: str) -> tuple[int, int] | None:
    """Size and modification time: whether a file is still the one this session read or wrote. None: no file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _has_signatures(doc) -> bool:
    """True when a signature field actually holds a signature (empty fields don't count). Reads the pages'
    lists of widgets without loading them: page.widgets() took seconds to open a big form.
    shortcut: works through MuPDF's objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    if (doc.get_sigflags() or 0) <= 0:
        return False
    m = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    for pno in range(len(doc)):
        for xref, kind, _ in doc.page_annot_xrefs(pno):
            if kind != pymupdf.PDF_ANNOT_WIDGET:
                continue
            obj = m.pdf_load_object(pdf, xref)
            try:
                if m.pdf_field_type(obj) == m.PDF_WIDGET_TYPE_SIGNATURE and m.pdf_signature_is_signed(pdf, obj):
                    return True
            except Exception:  # a field MuPDF cannot read holds no signature it can show
                continue
    return False


class SessionError(ValueError):
    """Error with a stable code the UI translates (NO_DOC, NOT_ALLOWED, BUSY, ...)."""


class Snapshot:
    """One undo step: the whole document as it was, with its page revisions.

    A big one (a scanned document) goes to an anonymous temporary file, which the system removes
    when it is closed or the program ends, so a long undo history does not fill the memory. It is
    encrypted there with a key that only this step holds in memory: the working copy of a protected
    file has no password.
    """

    def __init__(self, label: str, data: bytes, revs: list, sizes: list, state: int, fonts: bool):
        self.label, self.revs, self.sizes, self.state, self.fonts = label, revs, sizes, state, fonts
        self.size = len(data)
        self._file = self._key = None
        if self.size > SPILL_BYTES:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # comes with pyHanko

            try:
                self._key = AESGCM.generate_key(bit_length=256)  # one per step: a part's number is its nonce
                self._file = tempfile.TemporaryFile()
                aes, view = AESGCM(self._key), memoryview(data)
                for k, start in enumerate(range(0, self.size, _CHUNK)):
                    self._file.write(aes.encrypt(k.to_bytes(12, "big"), view[start:start + _CHUNK], None))
                data = b""
            except OSError:  # no room in the temp folder: memory still works
                self.close()
                self._file = None
        self._data = data

    @property
    def on_disk(self) -> bool:
        return self._file is not None

    @property
    def data(self) -> bytes:
        if self._file is None:
            return self._data
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aes, parts = AESGCM(self._key), []
        self._file.seek(0)
        while part := self._file.read(_CHUNK + 16):  # each part carries its 16-byte tag
            parts.append(aes.decrypt(len(parts).to_bytes(12, "big"), part, None))
        data = b"".join(parts)
        if len(data) != self.size:  # a file cut after a whole part decrypts without complaint: the length tells
            raise OSError("undo step damaged")
        return data

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _drop(stack: list) -> None:
    for snap in stack:
        snap.close()
    stack.clear()


class Edit:
    """Handed to an edit block so it can say which pages it changed."""

    def __init__(self):
        self.touched: set[int] | None = None  # None = unknown, treat every page as changed
        self.revs: list | None = None
        self.fonts = False

    def touch(self, *pages: int) -> None:
        self.touched = (self.touched or set()) | {int(p) for p in pages}


class Session:
    def __init__(self):
        self.lock = threading.RLock()
        self.doc: pymupdf.Document | None = None
        self.path: str | None = None
        self.name = ""
        self.revs: list[int] = []
        self.sizes: list[tuple[float, float]] = []
        self._rev_index: dict[int, int] = {}
        self.state = 0
        self.saved_state = 0
        self.undo_stack: list[Snapshot] = []
        self.redo_stack: list[Snapshot] = []
        self.info: dict = {}
        self.rights = pt.rights_for({})
        self.protection: dict | None = None
        self.fonts_embedded = False
        self.signed = False
        self.original: bytes | None = None  # the bytes of a signed file as opened, while the file holds them
        self.disk: tuple[int, int] | None = None  # the file as this session last read or wrote it (_stamp)
        self.scanned = False
        self.forms = False
        self.xfa = False
        self.job: str | None = None
        self._job_thread: threading.Thread | None = None
        self.doc_id = 0

    # --- lifecycle ------------------------------------------------------------

    def open(self, path: str, password: str | None = None) -> None:
        with self.lock:
            self._check_job()  # opening now would drop the result of the running OCR
            before = _stamp(path)
            doc, info = pt.load_for_editing(path, password)
            try:
                an.settle_fields(doc)
            except Exception:  # a broken field must not keep the file from opening
                logging.getLogger("aurora").exception("could not draw the form fields of %s", path)
            name = os.path.basename(path)
            if info.get("converted"):
                name = os.path.splitext(name)[0] + ".pdf"
            self._install(doc, info, None if info.get("converted") else os.path.abspath(path), name)
            self.disk = before
            if info.get("converted"):
                self.saved_state = -1  # never saved as PDF yet
            elif self.signed:  # Save As and a further signature copy these bytes, not what the file holds later
                with open(path, "rb") as f:
                    data = f.read()
                self.original = data if _stamp(path) == before else None  # changed while it opened: no copy

    def _install(self, doc, info: dict, path: str | None, name: str) -> None:
        self.close()
        self.doc, self.info, self.path, self.name = doc, info, path, name
        self.doc_id = next(_DOC_ID)
        self.revs = [next(_REV) for _ in range(len(doc))]
        self._reindex()
        self.sizes = [self._size(i) for i in range(len(doc))]
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.state = self.saved_state = next(_STATE)
        self.rights = pt.rights_for(info)
        self.protection = None
        if info.get("protected"):
            self.protection = {k: info.get(k) for k in ("user_pw", "owner_pw", "perms", "algorithm")}
        self.fonts_embedded = False
        self.signed = _has_signatures(doc)
        self.original = None
        self.scanned = ocr.looks_scanned(doc)
        self.forms = an.has_fields(doc)
        self.xfa = an.xfa_only(doc)

    def close(self) -> None:
        with self.lock:
            if self.doc is not None:
                try:
                    self.doc.close()
                except Exception:
                    pass
            self.doc = None
            self.path = None
            self.original = self.disk = None
            self.name = ""
            self.revs, self.sizes, self._rev_index = [], [], {}
            _drop(self.undo_stack)
            _drop(self.redo_stack)

    # --- helpers --------------------------------------------------------------

    def _size(self, i: int) -> tuple[float, float]:
        r = self.doc[i].rect
        return (round(r.width, 2), round(r.height, 2))

    def _reindex(self) -> None:
        self._rev_index = {rev: i for i, rev in enumerate(self.revs)}

    def require(self, right: str | None = None) -> pymupdf.Document:
        if self.doc is None:
            raise SessionError("NO_DOC")
        if right and not self.rights.get(right, True):
            raise SessionError("NOT_ALLOWED")
        return self.doc

    def page_of_rev(self, rev: int) -> int | None:
        return self._rev_index.get(int(rev))

    @property
    def dirty(self) -> bool:
        return self.doc is not None and self.state != self.saved_state

    def mark_changed(self) -> None:
        """A change outside the page content (e.g. new passwords) that still needs saving."""
        with self.lock:
            self.state = next(_STATE)

    # --- snapshots / undo -------------------------------------------------------

    def _snapshot(self, label: str) -> Snapshot:
        return Snapshot(label, self.doc.tobytes(), list(self.revs), list(self.sizes), self.state, self.fonts_embedded)

    def _restore(self, snap: Snapshot, doc=None) -> None:
        """Go back to ``snap`` (``doc``: opened from it already), which is used up by that (undo and redo keep a
        new one of the state they leave)."""
        old = self.doc
        self.doc = pymupdf.open("pdf", snap.data) if doc is None else doc
        if old is not None:
            old.close()
        self.revs, self.sizes, self.state, self.fonts_embedded = list(snap.revs), list(snap.sizes), snap.state, snap.fonts
        self._reindex()
        self.forms = an.has_fields(self.doc)
        snap.close()

    def _push_undo(self, snap: Snapshot) -> None:
        self.undo_stack.append(snap)
        total = sum(s.size for s in self.undo_stack)
        while len(self.undo_stack) > MAX_UNDO or (total > MAX_UNDO_BYTES and len(self.undo_stack) > 1):
            old = self.undo_stack.pop(0)
            total -= old.size
            old.close()

    def _check_job(self) -> None:
        if self.job and threading.current_thread() is not self._job_thread:
            raise SessionError("BUSY")

    @contextlib.contextmanager
    def edit(self, label: str, *, right: str | None = "modify", pages=None):
        """Run a change as one undo step. On any error the document is rolled back.

        Unless the block says otherwise (``ed.touch`` / ``ed.revs``), every page gets a
        new revision when the page count changed, and only touched pages otherwise.
        """
        with self.lock:
            self.require(right)
            self._check_job()
            snap = self._snapshot(label)
            ed = Edit()
            if pages is not None:
                ed.touched = {int(p) for p in pages}  # may be empty: no page content changed
            try:
                yield ed
            except BaseException:
                try:
                    self._restore(snap)
                except Exception:  # the step cannot be read back: keep the half-done change, shown and unsaved
                    logging.getLogger("aurora").exception("could not roll back %s", label)
                    snap.close()
                    self.revs = [next(_REV) for _ in range(len(self.doc))]
                    self.sizes = [self._size(i) for i in range(len(self.doc))]
                    self._reindex()
                    self.state = next(_STATE)
                raise
            self._commit(snap, ed)

    def _commit(self, snap: Snapshot, ed: Edit) -> None:
        self._push_undo(snap)
        _drop(self.redo_stack)
        n = len(self.doc)
        if ed.revs is not None:
            self.revs = list(ed.revs)
            changed = [i for i, r in enumerate(self.revs) if r is None]
            for i in changed:
                self.revs[i] = next(_REV)
            self.sizes = [self._size(i) for i in range(n)]
        elif n == len(snap.revs) and ed.touched is not None:
            for p in ed.touched:
                self.revs[p] = next(_REV)
                self.sizes[p] = self._size(p)
        else:
            self.revs = [next(_REV) for _ in range(n)]
            self.sizes = [self._size(i) for i in range(n)]
        self._reindex()
        if ed.fonts:
            self.fonts_embedded = True
        self.forms = an.has_fields(self.doc)
        self.state = next(_STATE)

    def undo(self) -> bool:
        return self._step(self.undo_stack, self.redo_stack)

    def redo(self) -> bool:
        return self._step(self.redo_stack, self.undo_stack)

    def _step(self, src: list, dst: list) -> bool:
        """Go to the state on top of ``src``, keeping the one left on ``dst``."""
        with self.lock:
            self.require()
            self._check_job()
            if not src:
                return False
            snap = src[-1]
            try:
                doc = pymupdf.open("pdf", snap.data)  # a damaged step fails here, before the document changes
            except Exception:
                logging.getLogger("aurora").warning("undo step %r cannot be read: dropped", snap.label, exc_info=True)
                src.pop().close()  # else it blocks every step below it; the document stays as it is
                raise SessionError("UNDO_DAMAGED") from None
            dst.append(self._snapshot(snap.label))
            src.pop()
            self._restore(snap, doc)
            return True

    # --- long page-by-page work (OCR) ------------------------------------------------

    def reserve_job(self, label: str) -> tuple[int, int]:
        """Take the job slot for ``label`` before its thread starts, so nothing changes the pages it was given
        meanwhile: edits, undo and opening a file are refused (BUSY) from now. Returns the document's identity,
        which :meth:`run_pages` checks (``reserved``) as it takes over the slot."""
        with self.lock:
            self.require()
            if self.job:
                raise SessionError("BUSY")
            self.job, self._job_thread = label, None
            return self.doc_id, self.state

    def release_job(self, label: str) -> None:
        """Give back a reservation whose thread never ran."""
        with self.lock:
            if self.job == label and self._job_thread is None:
                self.job = None

    def run_pages(self, label: str, pages, fn, progress=None, right: str = "modify", reserved=None) -> list[int]:
        """Apply ``fn(page) -> bool changed`` page by page as ONE undo step.

        The lock is released between pages so the viewer keeps rendering; other edits are
        refused (BUSY) until the run finishes. Cancelling keeps the pages already done.
        """
        with self.lock:
            if reserved is not None:
                self.release_job(label)
                if (self.doc_id, self.state) != tuple(reserved):
                    raise SessionError("STALE")  # another document, or this one changed (closed meanwhile)
            self.require(right)
            if self.job:
                raise SessionError("BUSY")
            snap = self._snapshot(label)
            self.job, self._job_thread = label, threading.current_thread()
        touched: list[int] = []
        try:
            for k, pno in enumerate(pages):
                if progress is not None and progress(k, len(pages)) is False:
                    break
                with self.lock:
                    if fn(self.doc[pno]):
                        touched.append(pno)
                time.sleep(0.01)  # the lock is not fair: give waiting requests (page images) a turn
            if progress is not None:
                progress(len(pages), len(pages))
        finally:
            with self.lock:
                self.job, self._job_thread = None, None
                if touched:
                    ed = Edit()
                    ed.touch(*touched)
                    self._commit(snap, ed)
                else:
                    snap.close()
        return touched

    # --- saving -------------------------------------------------------------------

    def save_protection(self) -> dict | None:
        """Protection to re-apply when writing this document back."""
        if not self.protection:
            return None
        p = dict(self.protection)
        if p.get("user_pw") is None:
            raise SessionError("NEED_PASSWORDS")
        if p.get("owner_pw") is None:
            # owner password unknown: keep the restrictions with an owner password nobody knows
            p["owner_pw"] = secrets.token_urlsafe(24)
        return p

    def write(self, out_path: str, *, protection: dict | None | str = "keep") -> None:
        """Write the current document to ``out_path`` atomically (temp file + replace)."""
        with self.lock:
            self.require()
            prot = self.save_protection() if protection == "keep" else protection
            pt.save_pdf(self.doc, out_path, subset_fonts=self.fonts_embedded, **pt.protection_args(prot))
            if self._is_path(out_path):
                self.original = None  # the file holds a rewrite now, and its signatures are broken
                self.disk = _stamp(out_path)

    def save(self) -> str:
        """Save over the current file, keeping its protection."""
        with self.lock:
            self.require()
            if not self.path:
                raise SessionError("NEED_PATH")
            if not self.dirty:
                return self.path  # nothing changed: leave the file, and any signature in it, alone
            if self.protection and self.protection.get("owner_pw") is None:
                raise SessionError("SAVE_AS_COPY")  # never rewrite a file whose owner password we lack
            if _stamp(self.path) not in (None, self.disk):
                raise SessionError("FILE_CHANGED")  # another program wrote it since (a sync, a colleague): keep that
            self.write(self.path)
            self.saved_state = self.state
            return self.path

    def _is_path(self, path: str) -> bool:
        return bool(self.path) and os.path.normcase(os.path.abspath(path)) == os.path.normcase(self.path)

    @property
    def copies_as_is(self) -> bool:
        """Save As copies the file byte for byte: it is signed and unchanged, and a rewrite would break the signatures."""
        return self.signed and not self.dirty and self.original is not None

    def save_as(self, out_path: str, protection: dict | None | str = "keep") -> str:
        """Save to a new file and continue working on it, with ``protection`` (default: the current one)."""
        with self.lock:
            self.require()
            if self.copies_as_is:  # the bytes as opened, also over the file itself if something replaced it since
                write_atomic(out_path, self.original)
            else:
                if self._is_path(out_path) and self.protection and self.protection.get("owner_pw") is None:
                    raise SessionError("SAVE_AS_COPY")
                # without the owner password the copy gets a random one, so its restrictions stay
                prot = self.save_protection() if protection == "keep" else protection
                self.write(out_path, protection=prot)
                self.protection = prot  # only after the write worked: later saves keep the same protection
                self.original = None
            self.path = os.path.abspath(out_path)
            self.name = os.path.basename(out_path)
            self.disk = _stamp(self.path)
            self.saved_state = self.state
            return self.path

    # --- read helpers for the UI ------------------------------------------------------

    def summary(self) -> dict:
        with self.lock:
            if self.doc is None:
                return {"open": False}
            prot = self.protection or {}
            return {
                "open": True,
                "id": self.doc_id,
                "name": self.name,
                "path": self.path or "",
                # [width, height, revision, xref]: links point at a page by its xref, which moves with the page
                "pages": [[w, h, rev, self.doc.page_xref(i)] for i, ((w, h), rev) in enumerate(zip(self.sizes, self.revs))],
                "dirty": self.dirty,
                "undo": self.undo_stack[-1].label if self.undo_stack else "",
                "redo": self.redo_stack[-1].label if self.redo_stack else "",
                "protected": bool(self.protection),
                "owner": bool(self.info.get("owner", True)) if self.protection else True,
                "needPasswords": self.protection is not None and prot.get("user_pw") is None,
                "algorithm": prot.get("algorithm") or "",
                "rights": dict(self.rights),
                "signed": self.signed,
                "forms": self.forms,
                "scanned": self.scanned,
                "xfa": self.xfa,
                "busy": self.job or "",
                "meta": pt.get_metadata(self.doc),
            }
