"""Per-user settings, recent files and the saved-signature library.

Stored in the user data folder (see core.paths.data_dir) because the window's web
storage is wiped between runs and is bound to a random local port anyway.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .paths import data_dir, write_atomic

_LOCK = threading.RLock()  # requests run on several threads: each read-change-write happens as one
MAX_RECENT = 12

DEFAULTS = {
    "lang": "en",  # until the user picks one, load() gives the language of Windows (system_lang)
    "theme": "system",  # system follows the Windows app colour
    "author": "",
    "night": False,
    "fit": "width",
    "ocr_langs": "",
    "sig_reason": "",
    "sig_location": "",
    "last_dir": "",
    "recent": [],
}


def _file() -> Path:
    return data_dir() / "settings.json"


# Windows primary language id -> UI language (Croatian and Bosnian share Serbian's id; js/lang has the rest)
PRIMARY = {0x14: "no", 0x1A: "sr", 0x1D: "sv", 0x06: "da", 0x07: "de", 0x08: "el", 0x10: "it", 0x0C: "fr",
           0x11: "ja", 0x0A: "es", 0x16: "pt", 0x19: "ru", 0x22: "uk", 0x2F: "mk", 0x0E: "hu", 0x02: "bg",
           0x18: "ro", 0x1F: "tr", 0x0B: "fi"}
# ISO 639-1 codes from the LANG variable that are not the UI code itself
ALIASES = {"nb": "no", "nn": "no", "hr": "sr", "bs": "sr"}


def lang_for(langid: int) -> str:
    """UI language for a Windows LANGID, English when the program does not have it."""
    if langid == 0x0416:
        return "pt-BR"
    if langid in (0x0804, 0x1004):  # Chinese in China and Singapore; the traditional script gets English
        return "zh-CN"
    return PRIMARY.get(langid & 0x3FF, "en")


def system_lang() -> str:
    """The language a first start speaks: that of Windows, or of the LANG variable elsewhere."""
    if os.name == "nt":
        import ctypes
        return lang_for(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    tag = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if tag.startswith("pt_BR"):
        return "pt-BR"
    if tag.startswith(("zh_CN", "zh_SG")):
        return "zh-CN"
    code = ALIASES.get(tag[:2].lower(), tag[:2].lower())
    return code if code in PRIMARY.values() else "en"


def load() -> dict:
    data = dict(DEFAULTS)
    stored = None
    with _LOCK:  # Windows cannot replace the file (save) while another thread has it open for reading
        try:
            stored = json.loads(_file().read_text(encoding="utf-8"))
        except OSError:
            pass
        except ValueError:  # damaged: set aside where it can still be read, not overwritten by the next save
            _set_aside(_file())
    if isinstance(stored, dict):
        data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if not isinstance(stored, dict) or "lang" not in stored:
        data["lang"] = system_lang()
    if not data["author"]:
        try:
            data["author"] = getpass.getuser()
        except Exception:
            data["author"] = ""
    return data


def _set_aside(path: Path) -> None:
    bad, k = path.with_name("settings.bad.json"), 1
    while bad.exists():
        k += 1
        bad = path.with_name(f"settings.bad{k}.json")
    try:
        os.replace(path, bad)
    except OSError:
        logging.getLogger("aurora").warning("could not set the damaged settings aside", exc_info=True)


def save(data: dict) -> None:
    """Remembering is a convenience: a file another program holds (backup, antivirus) must not turn the open,
    save or signing that asked for it into an error."""
    with _LOCK:
        try:
            write_atomic(_file(), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        except OSError:
            logging.getLogger("aurora").warning("could not write the settings", exc_info=True)


def update(**changes) -> dict:
    with _LOCK:
        data = load()
        for k, v in changes.items():
            if k in DEFAULTS and k != "recent":
                data[k] = v
        save(data)
    return data


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def add_recent(path: str, pages: int = 0) -> None:
    with _LOCK:
        data = load()
        items = [r for r in data.get("recent", []) if not _same(r.get("path", ""), path)]
        items.insert(0, {"path": path, "name": os.path.basename(path), "pages": pages, "t": int(time.time())})
        data["recent"] = items[:MAX_RECENT]
        data["last_dir"] = os.path.dirname(path)
        save(data)


def forget_recent(path: str) -> None:
    with _LOCK:
        data = load()
        data["recent"] = [r for r in data.get("recent", []) if not _same(r.get("path", ""), path)]
        save(data)


def recent() -> list[dict]:
    return [dict(r, exists=os.path.exists(r.get("path", ""))) for r in load().get("recent", [])]


# --- saved signatures (transparent PNGs) ------------------------------------------


def _sig_dir() -> Path:
    d = data_dir() / "signatures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sig_id_ok(ident: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{12}", ident or ""))


def list_signatures() -> list[dict]:
    files = sorted(_sig_dir().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"id": p.stem, "kind": "sig"} for p in files if _sig_id_ok(p.stem)]


def save_signature(png: bytes) -> str:
    if not png.startswith(b"\x89PNG"):
        raise ValueError("not a PNG")
    ident = uuid.uuid4().hex[:12]
    (_sig_dir() / f"{ident}.png").write_bytes(png)
    return ident


def signature_bytes(ident: str) -> bytes:
    if not _sig_id_ok(ident):
        raise ValueError("bad id")
    return (_sig_dir() / f"{ident}.png").read_bytes()


def remove_signature(ident: str) -> None:
    """Hide a saved signature (moved to signatures/removed, never destroyed)."""
    if not _sig_id_ok(ident):
        raise ValueError("bad id")
    src = _sig_dir() / f"{ident}.png"
    if src.exists():
        trash = _sig_dir() / "removed"
        trash.mkdir(exist_ok=True)
        shutil.move(str(src), str(trash / src.name))
