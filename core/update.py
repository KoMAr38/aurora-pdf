"""New versions of the copy installed with install.exe, from the GitHub Releases of the project.

The Microsoft Store updates its own copy, and its rules forbid a second way, so the interface
offers this only outside the MSIX package. Only install.exe from this repository's releases is
downloaded, over HTTPS; the installer then replaces the program and starts it again.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

from .paths import VERSION

REPO = "KoMAr38/aurora-pdf"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
ASSET = "install.exe"


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def latest(timeout: float = 10) -> dict:
    """{"version", "newer", "url", "size"} of the newest release; no release yet counts as up to date.
    Raises OSError when GitHub cannot be reached."""
    req = urllib.request.Request(LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": f"AuroraPDF/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # the repository has no release yet
            return {"version": VERSION, "newer": False, "url": "", "size": 0}
        raise
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == ASSET), None)
    tag = str(rel.get("tag_name") or "")
    url = str(asset.get("browser_download_url") or "") if asset else ""
    return {"version": tag.lstrip("vV"), "newer": bool(url.startswith(DOWNLOADS)) and version_tuple(tag) > version_tuple(VERSION),
            "url": url, "size": int(asset.get("size") or 0) if asset else 0}


def download(url: str, progress=None) -> str:
    """Save the installer in a temp folder of its own; ``progress(done, total)`` returns False to stop."""
    if not url.startswith(DOWNLOADS):
        raise ValueError("INVALID")  # only this project's release files
    from .pdftools import Cancelled

    folder = tempfile.mkdtemp(prefix="aurora_update_")
    path = os.path.join(folder, ASSET)
    req = urllib.request.Request(url, headers={"User-Agent": f"AuroraPDF/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress is not None and progress(done, total) is False:
                    raise Cancelled()
        if total and done != total:
            raise OSError(f"download stopped at {done} of {total} bytes")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # never leave half an installer behind
        raise
    return path


def run_installer(path: str) -> None:
    """Start the downloaded install.exe in its own window. It waits until this program has closed.
    PYINSTALLER_RESET_ENVIRONMENT keeps it from taking this frozen program's settings for its own."""
    subprocess.Popen([path], cwd=os.path.dirname(path), creationflags=subprocess.CREATE_NEW_CONSOLE,
                     env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"))
