"""Aurora PDF - the backend API the interface calls.

The page talks to this object over a token-protected local HTTP endpoint
(core.server): POST /<token>/api/<method> with a JSON body. Only functions
decorated with @rpc are reachable. Every method takes one dict and returns a dict;
errors come back as {"ok": False, "error": CODE} with stable codes the UI translates.
Edits answer with {"ok": True, "doc": <summary>} so the viewer can refresh exactly the
pages whose revision changed.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import sys
import webbrowser

import pymupdf

from core import annots as an
from core import ocr, settings, signing
from core import pdftools as pt
from core.geom import size_unit
from core.paths import VERSION
from core.jobs import Jobs
from core.session import Session, SessionError

log = logging.getLogger("aurora.api")

# pywebview takes a filter as "Description (*.a;*.b)" and refuses a description with anything
# other than letters, digits and spaces (webview.util.parse_file_type), so no "/" or "*" in the names
PDF_TYPES = ("PDF (*.pdf)",)
DOCX_TYPES = ("Word (*.docx)",)
PATTERNS = {"pdf": "*.pdf", "images": "*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp;*.gif;*.webp", "all": "*.*",
            "text": "*.txt", "id": "*.pfx;*.p12"}
FILTER_NAMES = {
    "sr": {"pdf": "PDF", "images": "Slike", "all": "Svi fajlovi", "text": "Tekst", "id": "Digitalni ID"},
    "en": {"pdf": "PDF", "images": "Images", "all": "All files", "text": "Text", "id": "Digital ID"},
    "no": {"pdf": "PDF", "images": "Bilder", "all": "Alle filer", "text": "Tekst", "id": "Digital ID"},
}

SUFFIX = {
    "sr": {"copy": "_kopija", "protected": "_zasticen", "unprotected": "_otkljucan", "small": "_kompresovan",
           "pages": "_strane", "signed": "_potpisan", "merged": "spojeno", "images": "slike"},
    "en": {"copy": "_copy", "protected": "_protected", "unprotected": "_unlocked", "small": "_compressed",
           "pages": "_pages", "signed": "_signed", "merged": "merged", "images": "images"},
    "no": {"copy": "_kopi", "protected": "_beskyttet", "unprotected": "_ulast", "small": "_komprimert",
           "pages": "_sider", "signed": "_signert", "merged": "sammenslatt", "images": "bilder"},
}

CODE = re.compile(r"[A-Z][A-Z_]+")
_RPC: dict = {}


def rpc(fn):
    _RPC[fn.__name__] = fn
    return fn


class Dialogs:
    """Native file dialogs through the pywebview window. Tests swap in a scripted queue."""

    def __init__(self, window=None):
        self.window = window
        self.script: list | None = None  # test mode: answers handed out in order

    def _next(self):
        return self.script.pop(0) if self.script else None

    def open(self, multiple=False, types=PDF_TYPES, directory="") -> list[str]:
        if self.script is not None:
            r = self._next()
            return list(r) if isinstance(r, (list, tuple)) else ([r] if r else [])
        import webview

        r = self.window.create_file_dialog(webview.FileDialog.OPEN, directory=directory or "",
                                           allow_multiple=multiple, file_types=types)
        return list(r) if r else []

    def save(self, filename, types=PDF_TYPES, directory="") -> str | None:
        if self.script is not None:
            return self._next()
        import webview

        r = self.window.create_file_dialog(webview.FileDialog.SAVE, directory=directory or "",
                                           save_filename=filename, file_types=types)
        if not r:
            return None
        path = r[0] if isinstance(r, (list, tuple)) else r
        ext = os.path.splitext(filename)[1]
        if ext and not path.lower().endswith(ext.lower()):
            path += ext
        return path

    def folder(self, directory="") -> str | None:
        if self.script is not None:
            return self._next()
        import webview

        r = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=directory or "")
        if not r:
            return None
        return r[0] if isinstance(r, (list, tuple)) else r


def _b64_payload(data_url: str) -> bytes:
    return base64.b64decode(data_url.split(",", 1)[1] if "," in data_url else data_url)


def _signature_png(image: bytes, *, clean: bool = True) -> bytes:
    """Trim a signature picture to its ink; with ``clean`` a light background turns transparent."""
    from PIL import Image

    img = Image.open(io.BytesIO(image)).convert("RGBA")
    if clean:
        gray = img.convert("L")
        alpha = gray.point(lambda v: 0 if v > 225 else min(255, int((225 - v) * 2.4)))
        if img.getchannel("A").getextrema()[0] < 255:  # keep existing transparency too
            from PIL import ImageChops

            alpha = ImageChops.multiply(alpha, img.getchannel("A"))
        img.putalpha(alpha)
    box = img.getchannel("A").getbbox()
    if not box:
        raise ValueError("EMPTY_SIGNATURE")
    pad = 6
    img = img.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(img.width, box[2] + pad), min(img.height, box[3] + pad)))
    if img.width > 1400:
        img = img.resize((1400, max(1, int(img.height * 1400 / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class Api:
    def __init__(self, session: Session | None = None, dialogs: Dialogs | None = None, *, test_mode: bool = False):
        self._session = session or Session()
        self._dialogs = dialogs or Dialogs()
        self._jobs = Jobs()
        self._window = None
        self._test_mode = test_mode
        self._quit = None  # set by main: callable that closes the window for real
        self._titlebar = None  # set by main on Windows: callable(dark) that recolours the title bar
        self.boot_files: list[str] = []  # set by main: files named on the command line

    # --- plumbing -----------------------------------------------------------------

    def attach_window(self, window) -> None:
        self._window = window
        self._dialogs.window = window

    def dispatch(self, name: str, payload: dict | None) -> dict:
        fn = _RPC.get(name)
        if fn is None or (name.startswith("test_") and not self._test_mode):
            return {"ok": False, "error": "NOT_FOUND"}
        try:
            res = fn(self, payload or {})
            return {"ok": True} if res is None else res
        except pt.Cancelled:
            return {"ok": False, "error": "CANCELLED"}
        except (SessionError, ValueError) as exc:
            msg = str(exc)
            if CODE.fullmatch(msg):
                return {"ok": False, "error": msg}
            log.warning("%s: %s", name, msg)
            return {"ok": False, "error": "INVALID", "message": msg}
        except FileNotFoundError as exc:
            return {"ok": False, "error": "FILE_NOT_FOUND", "message": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "error": "FILE_LOCKED", "message": str(exc)}
        except Exception as exc:  # unexpected: keep the app alive, log the trace
            log.exception("rpc %s failed", name)
            return {"ok": False, "error": "ERROR", "message": f"{type(exc).__name__}: {exc}"}

    @property
    def s(self) -> Session:
        return self._session

    def _doc(self, **extra) -> dict:
        out = {"ok": True, "doc": self.s.summary()}
        out.update(extra)
        return out

    def _lang(self) -> str:
        lang = settings.load()["lang"]
        return lang if lang in SUFFIX else "en"

    def _suffix(self, key: str) -> str:
        return SUFFIX[self._lang()][key]

    def _types(self, *kinds: str) -> tuple[str, ...]:
        names = FILTER_NAMES[self._lang()]
        return tuple(f"{names[k]} ({PATTERNS[k]})" for k in kinds)

    def _default_name(self, suffix_key: str, ext: str = ".pdf") -> str:
        base = os.path.splitext(self.s.name or "dokument.pdf")[0]
        return f"{base}{self._suffix(suffix_key)}{ext}"

    def _dir(self) -> str:
        if self.s.path:
            return os.path.dirname(self.s.path)
        return settings.load().get("last_dir", "") or ""

    def _is_open(self, path: str | None) -> bool:
        return bool(path and self.s.path) and os.path.normcase(os.path.abspath(path)) == os.path.normcase(self.s.path)

    def _new_file(self, out: str | None) -> str | None:
        """Where an export or copy goes. Never the open document's own file: the app would go on showing
        a document that file no longer holds (and a signed original would lose its signature)."""
        if self._is_open(out):
            raise SessionError("OPEN_FILE")
        return out

    @staticmethod
    def _owner_password(path: str, exc: ValueError) -> dict:
        """A protected file going into a document (insert, merge) takes its owner password: the interface
        asks for it and sends it along with ``path`` the next time. Any other error goes on up."""
        if str(exc) not in ("SOURCE_PROTECTED", "WRONG_PASSWORD"):
            raise exc
        return {"ok": False, "error": str(exc), "path": path, "name": os.path.basename(path)}

    def _author(self) -> str:
        return settings.load().get("author", "") or ""

    def _pages(self, spec, default_all=True) -> list[int]:
        n = len(self.s.require())
        if spec is None or spec == "":
            return list(range(n)) if default_all else []
        if isinstance(spec, str):
            try:
                return pt.parse_page_range(spec, n)
            except ValueError:
                raise SessionError("BAD_PAGES") from None
        pages = sorted({int(p) for p in spec})
        if not pages or pages[0] < 0 or pages[-1] >= n:
            raise SessionError("BAD_PAGES")
        return pages

    def _page_no(self, a: dict) -> int:
        doc = self.s.require()
        p = int(a.get("page", -1))
        if not 0 <= p < len(doc):
            raise SessionError("BAD_PAGES")
        if "rev" in a and self.s.revs[p] != int(a["rev"]):
            raise SessionError("STALE")
        return p

    # --- app state ------------------------------------------------------------------

    @rpc
    def state(self, a):
        return {
            "ok": True,
            "doc": self.s.summary(),
            "settings": {k: v for k, v in settings.load().items() if k != "recent"},
            "recent": settings.recent(),
            "ocrLangs": ocr.available_languages(),
            "jobs": self._jobs.running(),
            "version": VERSION,
        }

    @rpc
    def settings_set(self, a):
        allowed = {k: a[k] for k in ("lang", "theme", "author", "night", "fit", "ocr_langs", "sig_reason", "sig_location") if k in a}
        if allowed.get("theme", "system") not in ("system", "dark", "light"):
            raise ValueError("INVALID")
        return {"ok": True, "settings": {k: v for k, v in settings.update(**allowed).items() if k != "recent"}}

    @rpc
    def recent_forget(self, a):
        settings.forget_recent(a.get("path", ""))
        return {"ok": True, "recent": settings.recent()}

    # --- open / close ---------------------------------------------------------------

    def _open(self, path: str, password: str | None) -> dict:
        try:
            self.s.open(path, password)
        except ValueError as exc:
            code = str(exc)
            if code in ("NEED_PASSWORD", "WRONG_PASSWORD"):
                return {"ok": False, "error": code, "path": path, "name": os.path.basename(path)}
            raise
        if self.s.path:
            settings.add_recent(self.s.path, len(self.s.doc))
        return self._doc()

    @rpc
    def open_dialog(self, a):
        if self.s.job:
            raise SessionError("BUSY")
        paths = self._dialogs.open(False, self._types("pdf", "images", "all"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        return self._open(paths[0], None)

    @rpc
    def open_path(self, a):
        path = a.get("path", "")
        if not os.path.isfile(path):
            settings.forget_recent(path)
            raise FileNotFoundError(path)
        return self._open(path, a.get("password") or None)

    @rpc
    def boot(self, a):
        """Open a file passed on the command line (Open with... / double click)."""
        for arg in self.boot_files:
            if os.path.isfile(arg):
                return self._open(os.path.abspath(arg), None)
        return {"ok": False, "error": "CANCELLED"}

    @rpc
    def close_doc(self, a):
        if self.s.job:
            raise SessionError("BUSY")
        self.s.close()
        return self._doc()

    @rpc
    def quit(self, a):
        if self._quit:
            self._quit()
        return {"ok": True}

    @rpc
    def set_title(self, a):
        if self._window is not None:
            self._window.set_title(str(a.get("title", "Aurora PDF"))[:200])
        return {"ok": True}

    @rpc
    def toggle_fullscreen(self, a):
        if self._window is not None:
            self._window.toggle_fullscreen()
        return {"ok": True}

    @rpc
    def window_theme(self, a):
        """The page picked its colours; give the title bar the same ones."""
        if self._titlebar:
            self._titlebar(bool(a.get("dark", True)))
        return {"ok": True}

    @rpc
    def open_url(self, a):
        url = str(a.get("url", ""))
        if not re.match(r"^(https?://|mailto:)", url, re.I):
            raise SessionError("NOT_ALLOWED")
        webbrowser.open(url)
        return {"ok": True}

    @rpc
    def reveal(self, a):
        path = a.get("path") or self.s.path
        if not path or not os.path.exists(path):
            raise FileNotFoundError(path or "")
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # noqa: S606 - opens Explorer on the user's own folder
        return {"ok": True}

    # --- save -----------------------------------------------------------------------

    @rpc
    def save(self, a):
        path = self.s.save()
        settings.add_recent(path, len(self.s.doc))
        return self._doc(path=path)

    @rpc
    def save_as(self, a):
        self.s.require()
        # a missing open password is asked for before the file is picked
        prot = "keep" if self.s.copies_as_is else self._copy_protection(a)
        out = self._dialogs.save(self._default_name("copy") if self.s.path else self.s.name, PDF_TYPES, self._dir())
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        path = self.s.save_as(out, protection=prot)
        settings.add_recent(path, len(self.s.doc))
        return self._doc(path=path)

    def _protection_from(self, a: dict) -> dict:
        user_pw = a.get("user_pw") or ""
        owner_pw = a.get("owner_pw") or user_pw
        if not owner_pw:
            raise SessionError("NEED_PASSWORD")
        allowed = a.get("allowed")
        perms = pt.permissions_int(allowed if allowed is not None else pt.ALL_PERMS)
        return {"user_pw": user_pw, "owner_pw": owner_pw, "perms": perms,
                # RC4 is broken: only kept when a file that has it is saved again, never chosen for new protection
                "algorithm": a.get("algorithm") if a.get("algorithm") in ("aes256", "aes128") else "aes256"}

    @rpc
    def save_protected(self, a):
        return self._save_with(a, "protected", self._protection_from)

    @rpc
    def save_unprotected(self, a):
        return self._save_with(a, "unprotected", lambda a: None)

    def _save_with(self, a: dict, suffix_key: str, protection) -> dict:
        """A copy with new protection (``protection(a)``, None for none); picking the open file changes that one."""
        self.s.require()
        if self.s.protection and not self.s.info.get("owner"):
            raise SessionError("NOT_ALLOWED")  # only the owner may change the protection
        prot = protection(a)
        out = self._dialogs.save(self._default_name(suffix_key), PDF_TYPES, self._dir())
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        if self._is_open(out):
            return self._protect_open_file(prot)
        self.s.write(out, protection=prot)
        return self._doc(path=out)

    def _protect_open_file(self, protection: dict | None) -> dict:
        """The protect or unprotect dialog picked the open file itself: it becomes that version, and the
        session follows once the file is written, so the next save neither asks for old passwords nor
        brings them back. A write that fails (file open elsewhere) changes nothing."""
        with self.s.lock:
            self.s.write(self.s.path, protection=protection)
            self.s.protection = protection
            self.s.info.update(protected=bool(protection), owner=True)
            self.s.rights = pt.rights_for(self.s.info)
            self.s.saved_state = self.s.state
        return self._doc(path=self.s.path)

    @rpc
    def set_protection(self, a):
        """Answer to NEED_PASSWORDS (file opened with its owner password, open password unknown):
        drop the protection, or give the open password so the protection is kept as it is."""
        with self.s.lock:
            self.s.require()
            if a.get("remove"):
                if self.s.protection and not self.s.info.get("owner"):
                    raise SessionError("NOT_ALLOWED")
                self.s.protection = None
                self.s.mark_changed()
            else:
                pw = a.get("user_pw") or ""
                if not self.s.protection or (self.s.path and not pt.is_user_password(self.s.path, pw)):
                    raise SessionError("WRONG_PASSWORD")
                self.s.protection["user_pw"] = pw  # the file already has this protection: nothing new to save
        return self._doc()

    def _copy_protection(self, a: dict) -> dict | None:
        """Protection for a new file made from this document (extract, split, compress): its own, asked
        for before any dialog. Its owner may take a copy without it instead (``unprotected``), which
        leaves the open document as it is."""
        if a.get("unprotected"):
            if self.s.protection and not self.s.info.get("owner"):
                raise SessionError("NOT_ALLOWED")
            return None
        return self.s.save_protection()

    @rpc
    def unlock_owner(self, a):
        """Prove ownership of a protected file (owner password) to lift its restrictions."""
        with self.s.lock:
            self.s.require()
            if not self.s.path:
                raise SessionError("NEED_PATH")
            _, info = pt.load_for_editing(self.s.path, a.get("password") or "")
            if not info.get("owner"):
                raise SessionError("WRONG_PASSWORD")
            known_user = (self.s.protection or {}).get("user_pw")
            self.s.info.update(info)
            self.s.rights = pt.rights_for(info)
            self.s.protection = {k: info.get(k) for k in ("user_pw", "owner_pw", "perms", "algorithm")}
            if self.s.protection.get("user_pw") is None and known_user is not None:
                self.s.protection["user_pw"] = known_user
        return self._doc()

    # --- undo -----------------------------------------------------------------------

    @rpc
    def undo(self, a):
        self.s.undo()
        return self._doc()

    @rpc
    def redo(self, a):
        self.s.redo()
        return self._doc()

    # --- pages ----------------------------------------------------------------------

    @rpc
    def rotate(self, a):
        pages = self._pages(a.get("pages"))
        with self.s.edit("rotate", right="assemble", pages=pages):
            pt.rotate_pages(self.s.doc, pages, int(a.get("degrees", 90)))
        return self._doc()

    @rpc
    def delete_pages(self, a):
        pages = self._pages(a.get("pages"), default_all=False)
        if not pages:
            return self._doc()  # nothing to do: no undo step, nothing changed
        if len(pages) >= len(self.s.doc):
            raise SessionError("CANNOT_DELETE_ALL")
        gone = set(pages)
        with self.s.edit("delete", right="assemble") as ed:
            linked = pt.pages_linking_to(self.s.doc, gone)  # they lose those links: new revisions
            keep = [None if i in linked else r for i, r in enumerate(self.s.revs) if i not in gone]
            pt.delete_pages(self.s.doc, pages)
            ed.revs = keep
        return self._doc()

    @rpc
    def reorder(self, a):
        order = [int(i) for i in a.get("order", [])]
        with self.s.edit("reorder", right="assemble") as ed:
            revs = [self.s.revs[i] for i in order] if sorted(order) == list(range(len(self.s.revs))) else None
            pt.reorder_pages(self.s.doc, order)
            ed.revs = revs
        return self._doc()

    @rpc
    def insert_blank(self, a):
        doc = self.s.require("assemble")
        at = int(a.get("at", len(doc)))
        like = max(0, min(int(a.get("like", at - 1 if at > 0 else 0)), len(doc) - 1))
        r = doc[like].rect
        with self.s.edit("insert", right="assemble") as ed:
            revs = list(self.s.revs)
            pos = pt.insert_blank(self.s.doc, at, r.width, r.height)
            revs.insert(pos, None)
            ed.revs = revs
        return self._doc(at=pos)

    @rpc
    def insert_file_dialog(self, a):
        self.s.require("assemble")
        paths = [p for p in (a.get("paths") or []) if os.path.isfile(p)] \
            or self._dialogs.open(False, self._types("pdf", "images", "all"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        password = a.get("password") or None
        for p in paths:  # first: a wrong password must not cost a copy of the open document each time
            try:
                pt.check_source(p, password)
            except ValueError as exc:
                return self._owner_password(p, exc)
        at = int(a.get("at", len(self.s.doc)))
        with self.s.edit("insert", right="assemble") as ed:
            revs = list(self.s.revs)
            at = max(0, min(at, len(revs)))
            n = 0
            for p in paths:  # a PDF by what it holds, not by its name; an image becomes a page
                n += pt.insert_pdf_file(self.s.doc, p, at + n, password)
            ed.revs = revs[:at] + [None] * n + revs[at:]
        return self._doc(at=at, count=n)

    @rpc
    def insert_images_dialog(self, a):
        self.s.require("assemble")
        paths = self._dialogs.open(True, self._types("images"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        at = int(a.get("at", len(self.s.doc)))
        with self.s.edit("insert", right="assemble") as ed:
            revs = list(self.s.revs)
            at = max(0, min(at, len(revs)))
            n = pt.insert_images(self.s.doc, paths, at)
            ed.revs = revs[:at] + [None] * n + revs[at:]
        return self._doc(at=at, count=n)

    @rpc
    def duplicate(self, a):
        pages = self._pages(a.get("pages"), default_all=False)
        if not pages:
            return self._doc()
        with self.s.edit("duplicate", right="assemble") as ed:
            revs = list(self.s.revs)
            made = pt.duplicate_pages(self.s.doc, pages)
            for idx in made:
                revs.insert(idx, None)
            ed.revs = revs
        return self._doc()

    @rpc
    def crop(self, a):
        pages = self._pages(a.get("pages"))
        with self.s.edit("crop", pages=pages):
            pt.crop_pages(self.s.doc, pages, a.get("frac") or (0, 0, 1, 1))
        return self._doc()

    @rpc
    def uncrop(self, a):
        pages = self._pages(a.get("pages"))
        with self.s.edit("crop", pages=pages):
            pt.uncrop_pages(self.s.doc, pages)
        return self._doc()

    @rpc
    def extract_dialog(self, a):
        pages = self._pages(a.get("pages"))
        self.s.require("assemble")
        prot = self._copy_protection(a)
        out = self._new_file(self._dialogs.save(self._default_name("pages"), PDF_TYPES, self._dir()))
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        with self.s.lock:
            pt.extract_pages(self.s.doc, pages, out, prot)
        return {"ok": True, "path": out}

    @rpc
    def split_dialog(self, a):
        self.s.require("assemble")
        prot = self._copy_protection(a)
        folder = self._dialogs.folder(self._dir())
        if not folder:
            return {"ok": False, "error": "CANCELLED"}
        base = os.path.splitext(self.s.name or "dokument")[0]
        with self.s.lock:
            files = pt.split_doc(self.s.doc, folder, int(a.get("every", 1)), base, prot)
        return {"ok": True, "path": folder, "count": len(files)}

    # --- whole-document edits ---------------------------------------------------------

    @rpc
    def watermark_text(self, a):
        pages = self._pages(a.get("pages"))
        with self.s.edit("watermark", pages=pages) as ed:
            pt.watermark_text(
                self.s.doc, str(a.get("text", "")), pages=pages,
                opacity=float(a.get("opacity", 0.15)), rotate=int(a.get("angle", 45)),
                fontsize=float(a.get("size", 48)), color=a.get("color", "#8892a6"),
                tile=bool(a.get("tile")), behind=bool(a.get("behind")),
            )
            ed.fonts = True
        return self._doc()

    @rpc
    def watermark_image_dialog(self, a):
        self.s.require("modify")
        paths = self._dialogs.open(False, self._types("images"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        pages = self._pages(a.get("pages"))
        with open(paths[0], "rb") as f:
            data = f.read()
        with self.s.edit("watermark", pages=pages):
            pt.watermark_image(self.s.doc, data, pages=pages, opacity=float(a.get("opacity", 0.2)),
                               scale=float(a.get("scale", 0.45)), behind=bool(a.get("behind")))
        return self._doc()

    @rpc
    def header_footer(self, a):
        pages = self._pages(a.get("pages"))
        with self.s.edit("header", pages=pages) as ed:
            pt.add_header_footer(
                self.s.doc, text=str(a.get("text", "{n} / {total}")), pages=pages,
                position=a.get("position", "bc"), fontsize=float(a.get("size", 10)),
                color=a.get("color", "#444444"), margin=float(a.get("margin", 28)),
                start=int(a.get("start", 1)), file_name=self.s.name, date_text=str(a.get("date") or ""),
            )
            ed.fonts = True
        return self._doc()

    @rpc
    def set_metadata(self, a):
        with self.s.edit("metadata", pages=[]):
            pt.set_metadata(self.s.doc, a)
        return self._doc()

    # --- annotations ----------------------------------------------------------------

    @rpc
    def annot_add(self, a):
        p = self._page_no(a)
        kind = a.get("kind", "")
        right = "annotate" if kind != "redact" else "modify"
        author = self._author()
        with self.s.edit(kind, right=right, pages=[p]):
            page = self.s.doc[p]
            if kind == "ink":
                xref = an.add_ink(page, a["strokes"], color=a.get("color", "#e11d48"), width=float(a.get("width", 2)),
                                  opacity=float(a.get("opacity", 1)), author=author)
            elif kind in ("rect", "ellipse", "line", "arrow"):
                xref = an.add_shape(page, kind, a["coords"], color=a.get("color", "#2563eb"), fill=a.get("fill"),
                                    width=float(a.get("width", 2)), opacity=float(a.get("opacity", 1)), author=author)
            elif kind in an.MARKUP_ADDERS:
                xref = an.add_markup(page, kind, a["words"], color=a.get("color"), opacity=float(a.get("opacity", 1)),
                                     author=author)
            elif kind == "text":
                xref = an.add_freetext(page, a["rect"], a.get("text", ""), fontsize=float(a.get("size", 12)),
                                       color=a.get("color", "#111827"), fill=a.get("fill"), author=author)
            elif kind == "note":
                xref = an.add_note(page, a["point"], a.get("text", ""), color=a.get("color", "#facc15"), author=author)
            elif kind == "image":
                data = settings.signature_bytes(a["sig"]) if a.get("sig") else _b64_payload(a["png"])
                xref = an.add_image_stamp(page, a["rect"], data, author=author, label=a.get("label", ""))
            elif kind == "redact":
                for rect in a.get("rects") or [a["rect"]]:  # a text selection sends all its word boxes at once
                    xref = an.mark_redaction(page, rect, author=author)
            else:
                raise SessionError("BAD_KIND")
        return self._doc(xref=xref)

    @rpc
    def annot_move(self, a):
        p = self._page_no(a)
        with self.s.edit("move", right="annotate", pages=[p]):
            an.move_annot(self.s.doc[p], int(a["xref"]), float(a["dx"]), float(a["dy"]))
        return self._doc()

    @rpc
    def annot_resize(self, a):
        p = self._page_no(a)
        with self.s.edit("resize", right="annotate", pages=[p]):
            an.resize_annot(self.s.doc[p], int(a["xref"]), a["rect"])
        return self._doc()

    @rpc
    def annot_edit(self, a):
        p = self._page_no(a)
        with self.s.edit("edit", right="annotate", pages=[p]):
            an.edit_annot(self.s.doc[p], int(a["xref"]), content=a.get("content"), color=a.get("color"),
                          opacity=a.get("opacity"), width=a.get("width"))
        return self._doc()

    @rpc
    def annot_delete(self, a):
        p = self._page_no(a)
        with self.s.edit("delete", right="annotate", pages=[p]):
            an.delete_annot(self.s.doc[p], int(a["xref"]))
        return self._doc()

    @rpc
    def annots_list(self, a):
        with self.s.lock:
            return {"ok": True, "items": an.doc_annots(self.s.require())}

    @rpc
    def redact_search(self, a):
        q = str(a.get("query", "")).strip()
        if not q:
            raise SessionError("INVALID")
        with self.s.edit("redact") as ed:
            n = an.mark_redaction_text(self.s.doc, q, author=self._author())
            if not n:
                raise SessionError("NOTHING_FOUND")
        return self._doc(count=n)

    @rpc
    def redact_apply(self, a):
        with self.s.edit("redact"):
            n = an.apply_redactions(self.s.doc)
            if not n:
                raise SessionError("NOTHING_FOUND")
        return self._doc(count=n)

    @rpc
    def redact_count(self, a):
        with self.s.lock:
            return {"ok": True, "count": an.count_redactions(self.s.require())}

    @rpc
    def scrub(self, a):
        with self.s.edit("scrub"):
            self.s.doc.scrub(attached_files=bool(a.get("attachments", True)), embedded_files=bool(a.get("attachments", True)),
                             metadata=True, xml_metadata=True, javascript=True, thumbnails=True,
                             reset_fields=False, redactions=False, remove_links=False, hidden_text=False, clean_pages=False)
        return self._doc()

    # --- forms ----------------------------------------------------------------------

    @rpc
    def field_set(self, a):
        p = self._page_no(a)
        with self.s.edit("form", right="form", pages=[]) as ed:
            ed.touch(*an.set_field(self.s.doc[p], int(a["xref"]), a.get("value")))
        return self._doc()

    @rpc
    def fields_reset(self, a):
        with self.s.edit("form", right="form"):
            an.reset_fields(self.s.doc)
        return self._doc()

    @rpc
    def flatten(self, a):
        with self.s.edit("flatten"):
            an.flatten(self.s.doc)
        return self._doc()

    # --- text, search, outline, attachments, info ------------------------------------------

    @rpc
    def search(self, a):
        with self.s.lock:
            self.s.require()
            hits = pt.search_rects(self.s.doc, str(a.get("query", "")))
        return {"ok": True, "hits": hits}

    @rpc
    def page_text(self, a):
        with self.s.lock:
            doc = self.s.require()
            p = int(a.get("page", 0))
            return {"ok": True, "text": doc[p].get_text() if 0 <= p < len(doc) else ""}

    @rpc
    def toc(self, a):
        with self.s.lock:
            return {"ok": True, "items": pt.get_toc(self.s.require())}

    @rpc
    def toc_set(self, a):
        with self.s.edit("bookmarks", right="modify", pages=[]):
            pt.set_toc(self.s.doc, a.get("items", []))
        return self._doc(items=pt.get_toc(self.s.doc))

    @rpc
    def attachments(self, a):
        with self.s.lock:
            return {"ok": True, "items": pt.list_attachments(self.s.require())}

    @rpc
    def attachment_add_dialog(self, a):
        self.s.require("modify")
        paths = self._dialogs.open(False, self._types("all"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        with self.s.edit("attachment", pages=[]):
            pt.add_attachment(self.s.doc, paths[0])
        return self._doc(items=pt.list_attachments(self.s.doc))

    @rpc
    def attachment_save_dialog(self, a):
        name = a.get("name", "")
        with self.s.lock:
            names = {x["name"]: x for x in pt.list_attachments(self.s.require())}
        if name not in names:
            raise SessionError("NOT_FOUND")
        out = self._new_file(self._dialogs.save(names[name]["file"], self._types("all"), self._dir()))
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        with self.s.lock:
            pt.save_attachment(self.s.doc, name, out)
        return {"ok": True, "path": out}

    @rpc
    def attachment_delete(self, a):
        with self.s.edit("attachment", pages=[]):
            pt.delete_attachment(self.s.doc, a.get("name", ""))
        return self._doc(items=pt.list_attachments(self.s.doc))

    @rpc
    def doc_info(self, a):
        with self.s.lock:
            doc = self.s.require()
            meta = pt.get_metadata(doc)
            size = os.path.getsize(self.s.path) if self.s.path and os.path.exists(self.s.path) else 0
            first = doc[0].rect if len(doc) else pymupdf.Rect()
            return {
                "ok": True, "meta": meta, "pages": len(doc), "size": size, "path": self.s.path or "",
                "format": meta.get("format", ""), "pageSize": [round(first.width, 1), round(first.height, 1)],
                "fonts": pt.fonts_used(doc), "attachments": len(doc.embfile_names()),
                "toc": len(doc.get_toc()), "forms": self.s.forms, "signed": self.s.signed,
            }

    # --- jobs ---------------------------------------------------------------------------

    @rpc
    def job_status(self, a):
        st = self._jobs.status(a.get("id", ""))
        if st.get("state") != "running" and st.get("kind") == "ocr":  # also after an error: done pages changed
            st["doc"] = self.s.summary()
        return {"ok": True, "job": st}

    @rpc
    def job_cancel(self, a):
        self._jobs.cancel(a.get("id", ""))
        return {"ok": True}

    @rpc
    def ocr_start(self, a):
        langs = "+".join(code for code in str(a.get("langs", "eng")).split("+") if code in ocr.LANGUAGES) or "eng"
        dpi = max(72, min(int(a.get("dpi", 300)), 600))
        with self.s.lock:  # the pages and the job slot in one step: nothing moves the pages before the job runs
            doc = self.s.require("modify")
            pages = self._pages(a.get("pages"))
            if not a.get("force"):
                pages = [p for p in pages if not ocr.page_has_text(doc[p])]
            if not pages:
                raise SessionError("NOTHING_TO_OCR")
            reserved = self.s.reserve_job("ocr")

        def work(job):
            words = 0

            def one(page):
                nonlocal words
                n = ocr.ocr_page(page, langs, dpi)
                words += n
                return n > 0

            touched = self.s.run_pages("ocr", pages, one, progress=job.progress, reserved=reserved)
            with self.s.lock:
                self.s.scanned = ocr.looks_scanned(self.s.doc)
            return {"pages": len(touched), "words": words}

        try:
            settings.update(ocr_langs=langs)
            return {"ok": True, "job": self._jobs.start("ocr", work), "total": len(pages)}
        except BaseException:
            self.s.release_job("ocr")
            raise

    @rpc
    def export_start(self, a):
        """Exports that write new files: docx, txt, images, embedded images, compressed copy."""
        doc = self.s.require()
        kind = a.get("kind")
        base = os.path.splitext(self.s.name or "dokument")[0]
        if kind in ("txt", "docx", "images", "embedded") and not self.s.rights.get("copy"):
            raise SessionError("NOT_ALLOWED")  # all of them take the content out of the document
        with self.s.lock:
            pages = self._pages(a.get("pages"))
            seen = self.s.doc_id, self.s.state
        prot = self._copy_protection(a) if kind == "compress" else None
        if kind == "docx":
            out = self._dialogs.save(base + ".docx", DOCX_TYPES, self._dir())
        elif kind == "txt":
            out = self._dialogs.save(base + ".txt", self._types("text"), self._dir())
        elif kind == "compress":
            out = self._new_file(self._dialogs.save(self._default_name("small"), PDF_TYPES, self._dir()))
        elif kind in ("images", "embedded"):
            out = self._dialogs.folder(self._dir())
        else:
            raise SessionError("BAD_KIND")
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        dpi = max(72, min(int(a.get("dpi", 150)), 600))
        # the export works on a snapshot taken now, so it holds the lock only page by page and the viewer goes on
        # drawing; the pages were picked before the dialog, from the document as it was then
        with self.s.lock:
            if self.s.doc is None or (self.s.doc_id, self.s.state) != seen:
                raise SessionError("STALE")
            if kind == "compress":
                snap = self.s.doc.tobytes()
            elif kind == "docx":
                snap = pt.copy_pages(self.s.doc, pages)
            else:  # the whole document: pages keep their numbers and images their xrefs in the file names
                snap = pymupdf.open("pdf", self.s.doc.tobytes())

        def work(job):
            lock = self.s.lock
            try:
                if kind == "docx":
                    # pdf2docx reads a file: the snapshot goes to the temp folder under a password of its own, so
                    # a protected document does not lie there readable; the conversion runs in another process
                    import secrets
                    import tempfile

                    pw = secrets.token_urlsafe(18)
                    fd, tmp = tempfile.mkstemp(suffix=".pdf")
                    os.close(fd)
                    try:
                        with lock:
                            pt.save_pdf(snap, tmp, user_pw=pw, owner_pw=pw)
                        pt.export_docx(tmp, out, progress=job.progress, password=pw)
                    finally:
                        os.remove(tmp)
                    return {"path": out}
                if kind == "txt":
                    pt.export_text(snap, out, pages, lock=lock)
                    return {"path": out}
                if kind == "compress":
                    before, size = pt.compress_to(snap, out, image_dpi=int(a.get("dpi", 150)),
                                                  quality=int(a.get("quality", 75)), protection=prot,
                                                  progress=job.progress, lock=lock)
                    return {"path": out, "before": before, "after": size}
                if kind == "images":
                    files = pt.export_page_images(snap, pages, out, base, a.get("format", "png"), dpi,
                                                  progress=job.progress, lock=lock)
                    return {"path": out, "count": len(files)}
                files = pt.extract_embedded_images(snap, pages, out, base, progress=job.progress, lock=lock)
                return {"path": out, "count": len(files)}
            finally:
                if not isinstance(snap, bytes):
                    with lock:
                        snap.close()

        return {"ok": True, "job": self._jobs.start(f"export_{kind}", work)}

    # --- combine files ---------------------------------------------------------------

    @rpc
    def merge_dialog(self, a):
        """Pick files for merging; the panel collects them across several picks."""
        paths = self._dialogs.open(True, PDF_TYPES, self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        return {"ok": True, "paths": paths, "names": [os.path.basename(p) for p in paths]}

    @rpc
    def merge_run(self, a):
        paths = [p for p in a.get("paths", []) if os.path.isfile(p)]
        if len(paths) < 2:
            raise SessionError("NEED_TWO")
        if self.s.dirty and any(self._is_open(p) for p in paths):
            raise SessionError("SAVE_FIRST")  # its file does not hold the changes on screen yet
        passwords = a.get("passwords") or {}
        for p in paths:  # every owner password first, then where the result goes
            try:
                pt.check_source(p, passwords.get(p))
            except ValueError as exc:
                return self._owner_password(p, exc)
        out = self._new_file(self._dialogs.save(self._suffix("merged") + ".pdf", PDF_TYPES, os.path.dirname(paths[0])))
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        with self.s.lock:
            n = pt.merge(paths, out, passwords)
        return {"ok": True, "path": out, "pages": n}

    @rpc
    def images_to_pdf_dialog(self, a):
        imgs = self._dialogs.open(True, self._types("images"), self._dir())
        if not imgs:
            return {"ok": False, "error": "CANCELLED"}
        out = self._new_file(self._dialogs.save(self._suffix("images") + ".pdf", PDF_TYPES, os.path.dirname(imgs[0])))
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        with self.s.lock:
            n = pt.images_to_pdf(imgs, out)
        return {"ok": True, "path": out, "pages": n}

    # --- visual signatures (Fill & Sign) ----------------------------------------------------

    @rpc
    def sig_list(self, a):
        return {"ok": True, "items": settings.list_signatures()}

    @rpc
    def sig_save(self, a):
        png = _signature_png(_b64_payload(a.get("png", "")), clean=bool(a.get("clean", False)))
        return {"ok": True, "id": settings.save_signature(png), "items": settings.list_signatures()}

    @rpc
    def sig_image_dialog(self, a):
        paths = self._dialogs.open(False, self._types("images"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        with open(paths[0], "rb") as f:
            png = _signature_png(f.read(), clean=True)
        return {"ok": True, "id": settings.save_signature(png), "items": settings.list_signatures()}

    @rpc
    def sig_remove(self, a):
        settings.remove_signature(a.get("id", ""))
        return {"ok": True, "items": settings.list_signatures()}

    # --- digital IDs and certificate signatures ---------------------------------------------

    @rpc
    def ids_list(self, a):
        return {"ok": True, "items": signing.list_ids()}

    @rpc
    def id_create(self, a):
        info = signing.create_id(a.get("name", ""), a.get("password", ""), email=a.get("email", ""), org=a.get("org", ""))
        return {"ok": True, "id": info["id"], "items": signing.list_ids()}

    @rpc
    def id_import_dialog(self, a):
        paths = a.get("paths") or self._dialogs.open(False, self._types("id"), self._dir())
        if not paths:
            return {"ok": False, "error": "CANCELLED"}
        info = signing.import_id(paths[0], a.get("password", ""))
        return {"ok": True, "id": info["id"], "items": signing.list_ids()}

    @rpc
    def id_remove(self, a):
        signing.remove_id(a.get("id", ""))
        return {"ok": True, "items": signing.list_ids()}

    @rpc
    def sign_digital(self, a):
        """Sign the current document with a Digital ID into a new file, then open it."""
        doc = self.s.require()
        field = a.get("field") or None
        with self.s.lock:
            empty = bool(field) and field in {w.field_name for page in doc
                                              for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE])
                                              if not w.is_signed}
        # the form's own empty signature field is filled in; a new signature field changes the form itself
        for right in ("form",) if empty else ("modify", "annotate"):
            self.s.require(right)
        signer = signing.load_signer(a.get("id", ""), a.get("password", ""))
        # an unchanged signed file is signed on top of its own bytes, so earlier signatures stay valid
        original = self.s.original if self.s.copies_as_is else None
        on_top = original is not None
        known = self.s.protection or {}
        file_pw = (known.get("owner_pw") or known.get("user_pw")) if on_top and self.s.protection else None
        prot = None if on_top else self.s.save_protection()
        labels = a.get("labels") or {}
        name = signing.signer_name(signer)
        out = self._new_file(self._dialogs.save(self._default_name("signed"), PDF_TYPES, self._dir()))
        if not out:
            return {"ok": False, "error": "CANCELLED"}
        reason, location = a.get("reason", ""), a.get("location", "")
        settings.update(sig_reason=reason, sig_location=location)
        with self.s.lock:
            box = ap = None
            page_index = 0
            if a.get("rect"):
                page_index = self._page_no({"page": a.get("page", 0)})
                page = doc[page_index]
                box = signing.box_from_display(page, a["rect"])
                import datetime

                when = labels.get("when") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                lines = [(labels.get("signedBy", "Digitally signed by"), 7.5, "#5b6474"), (name, 12, "#0b1220"),
                         (f"{labels.get('date', 'Date')}: {when}", 7.5, "#5b6474")]
                if reason:
                    lines.append((f"{labels.get('reason', 'Reason')}: {reason}", 7.5, "#5b6474"))
                if location:
                    lines.append((f"{labels.get('location', 'Location')}: {location}", 7.5, "#5b6474"))
                image = settings.signature_bytes(a["sig"]) if a.get("sig") else None
                ap = signing.appearance_pdf(box[2] - box[0], box[3] - box[1], lines=lines, image=image,
                                            rotate=page.rotation, unit=size_unit(page))
            base = original if on_top else pt.clean_bytes(doc, self.s.fonts_embedded)
        signing.sign_pdf(base, out, signer, page_index=page_index, box=box, appearance_pdf=ap, reason=reason,
                         location=location, protection=prot, password=file_pw, field_name=field)
        password = (known.get("user_pw") or file_pw) if on_top else (prot.get("user_pw") if prot else None)
        res = self._open(out, password or None)
        res["path"] = out
        return res

    @rpc
    def signatures_validate(self, a):
        with self.s.lock:
            self.s.require()
            path = self.s.path
            prot = self.s.protection or {}
            data = self.s.original  # what was opened, also if the file was replaced since
        if data is None:
            if not path or not os.path.exists(path):
                return {"ok": True, "items": []}
            with open(path, "rb") as f:
                data = f.read()
        return {"ok": True, "items": signing.validate(data, prot.get("owner_pw") or prot.get("user_pw")),
                "dirty": self.s.dirty}

    # --- test hooks (only with test_mode) ------------------------------------------------------

    @rpc
    def test_dialogs(self, a):
        self._dialogs.script = list(a.get("answers", []))
        return {"ok": True}
