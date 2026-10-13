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
import dataclasses
import itertools
import os
import secrets
import shutil
import threading
import time

import pymupdf

from . import annots as an
from . import ocr
from . import pdftools as pt

_REV = itertools.count(1)
_STATE = itertools.count(1)
_DOC_ID = itertools.count(1)
MAX_UNDO = 60
MAX_UNDO_BYTES = 700 * 1024 * 1024


def _has_signatures(doc) -> bool:
    """True when a signature field actually holds a signature (empty fields don't count)."""
    if (doc.get_sigflags() or 0) <= 0:
        return False
    for page in doc:
        if any(w.is_signed for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE])):
            return True
    return False


class SessionError(ValueError):
    """Error with a stable code the UI translates (NO_DOC, NOT_ALLOWED, BUSY, ...)."""


def _atomic(out_path: str, produce) -> None:
    """``produce(tmp)`` writes a temp file next to ``out_path``, which then replaces it in one step."""
    tmp = out_path + ".aurora-tmp"
    try:
        produce(tmp)
        os.replace(tmp, out_path)
    except PermissionError as exc:
        raise SessionError("FILE_LOCKED") from exc
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@dataclasses.dataclass
class Snapshot:
    label: str
    data: bytes
    revs: list
    sizes: list
    state: int
    fonts: bool


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
        self.scanned = False
        self.forms = False
        self.job: str | None = None
        self._job_thread: threading.Thread | None = None
        self.doc_id = 0

    # --- lifecycle ------------------------------------------------------------

    def open(self, path: str, password: str | None = None) -> None:
        with self.lock:
            self._check_job()  # opening now would drop the result of the running OCR
            doc, info = pt.load_for_editing(path, password)
            name = os.path.basename(path)
            if info.get("converted"):
                name = os.path.splitext(name)[0] + ".pdf"
            self._install(doc, info, None if info.get("converted") else os.path.abspath(path), name)
            if info.get("converted"):
                self.saved_state = -1  # never saved as PDF yet

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
        self.scanned = ocr.looks_scanned(doc)
        self.forms = an.has_fields(doc)

    def close(self) -> None:
        with self.lock:
            if self.doc is not None:
                try:
                    self.doc.close()
                except Exception:
                    pass
            self.doc = None
            self.path = None
            self.name = ""
            self.revs, self.sizes, self._rev_index = [], [], {}
            self.undo_stack.clear()
            self.redo_stack.clear()

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

    def _restore(self, snap: Snapshot) -> None:
        old = self.doc
        self.doc = pymupdf.open("pdf", snap.data)
        if old is not None:
            old.close()
        self.revs, self.sizes, self.state, self.fonts_embedded = list(snap.revs), list(snap.sizes), snap.state, snap.fonts
        self._reindex()
        self.forms = an.has_fields(self.doc)

    def _push_undo(self, snap: Snapshot) -> None:
        self.undo_stack.append(snap)
        total = sum(len(s.data) for s in self.undo_stack)
        while len(self.undo_stack) > MAX_UNDO or (total > MAX_UNDO_BYTES and len(self.undo_stack) > 1):
            total -= len(self.undo_stack.pop(0).data)

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
                self._restore(snap)
                raise
            self._commit(snap, ed)

    def _commit(self, snap: Snapshot, ed: Edit) -> None:
        self._push_undo(snap)
        self.redo_stack.clear()
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
        with self.lock:
            self.require()
            self._check_job()
            if not self.undo_stack:
                return False
            snap = self.undo_stack.pop()
            self.redo_stack.append(self._snapshot(snap.label))
            self._restore(snap)
            return True

    def redo(self) -> bool:
        with self.lock:
            self.require()
            self._check_job()
            if not self.redo_stack:
                return False
            snap = self.redo_stack.pop()
            self.undo_stack.append(self._snapshot(snap.label))
            self._restore(snap)
            return True

    # --- long page-by-page work (OCR) ------------------------------------------------

    def run_pages(self, label: str, pages, fn, progress=None, right: str = "modify") -> list[int]:
        """Apply ``fn(page) -> bool changed`` page by page as ONE undo step.

        The lock is released between pages so the viewer keeps rendering; other edits are
        refused (BUSY) until the run finishes. Cancelling keeps the pages already done.
        """
        with self.lock:
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

    def write(self, out_path: str, *, protection: dict | None | str = "keep", compress: bool = False) -> None:
        """Write the current document to ``out_path`` atomically (temp file + replace)."""
        with self.lock:
            self.require()
            prot = self.save_protection() if protection == "keep" else protection
            prot = prot or {}
            _atomic(out_path, lambda tmp: pt.save_pdf(
                self.doc, tmp, user_pw=prot.get("user_pw"), owner_pw=prot.get("owner_pw"),
                perms=prot.get("perms") if prot.get("perms") not in (None, -1) else None,
                algorithm=prot.get("algorithm") or "aes256", compress=compress,
                subset_fonts=self.fonts_embedded,
            ))

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
            self.write(self.path)
            self.saved_state = self.state
            return self.path

    def save_as(self, out_path: str) -> str:
        """Save to a new file and continue working on it."""
        with self.lock:
            self.require()
            same = bool(self.path) and os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(self.path)
            if self.signed and not self.dirty and self.path:
                # an unchanged signed file is copied byte for byte: rewriting it would break the signatures
                if not same:
                    _atomic(out_path, lambda tmp: shutil.copyfile(self.path, tmp))
            else:
                if same and self.protection and self.protection.get("owner_pw") is None:
                    raise SessionError("SAVE_AS_COPY")
                # without the owner password the copy gets a random one, so its restrictions stay
                prot = self.save_protection()
                self.write(out_path, protection=prot)
                if prot:
                    self.protection = prot  # only after the write worked: later saves keep the same protection
            self.path = os.path.abspath(out_path)
            self.name = os.path.basename(out_path)
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
                "pages": [[w, h, rev] for (w, h), rev in zip(self.sizes, self.revs)],
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
                "busy": self.job or "",
                "meta": pt.get_metadata(self.doc),
            }
