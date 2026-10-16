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

from .paths import data_dir

_LOCK = threading.RLock()  # requests run on several threads: each read-change-write happens as one
MAX_RECENT = 12

DEFAULTS = {
    "lang": "sr",
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


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with _LOCK:  # Windows cannot replace the file (save) while another thread has it open for reading
            stored = json.loads(_file().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not data["author"]:
        try:
            data["author"] = getpass.getuser()
        except Exception:
            data["author"] = ""
    return data


def save(data: dict) -> None:
    """Remembering is a convenience: a file another program holds (backup, antivirus) must not turn the open,
    save or signing that asked for it into an error."""
    with _LOCK:
        try:
            tmp = _file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, _file())
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
