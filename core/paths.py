"""Where bundled resources live and where per-user data is written."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def app_root() -> Path:
    """Project folder when run from source; the unpacked bundle when frozen."""
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else Path(__file__).resolve().parent.parent


def resource(*parts: str) -> Path:
    return app_root().joinpath(*parts)


VERSION = "2.0.1"  # the Store package is this + ".0" (packaging/AppxManifest.xml; a test keeps them equal)
WEB_DIR = resource("web")
FONT_SANS = resource("resources", "fonts", "NotoSans-Regular.ttf")
TESSDATA = resource("resources", "tessdata")


def data_dir() -> Path:
    """Writable per-user folder (%APPDATA%\\AuroraPDF on Windows).

    AURORA_DATA_DIR overrides it, which the tests use to stay out of the real profile.
    """
    env = os.environ.get("AURORA_DATA_DIR")
    if env:
        path = Path(env)
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA") or Path.home()) / "AuroraPDF"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "AuroraPDF"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_atomic(out_path, produce) -> None:
    """Write ``out_path`` all or nothing: ``produce(tmp)`` (or the bytes given instead) goes to a temp file of a
    name of its own next to it, which is flushed to the disk and then replaces ``out_path`` in one step. A failure
    leaves ``out_path`` as it was and no temp file behind. PermissionError: ``out_path`` is held by another program."""
    out_path = os.fspath(out_path)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(out_path) + ".", suffix=".aurora-tmp",
                               dir=os.path.dirname(os.path.abspath(out_path)))
    try:
        if isinstance(produce, (bytes, bytearray)):
            with os.fdopen(fd, "wb") as f:
                f.write(produce)
        else:
            os.close(fd)
            produce(tmp)
        with open(tmp, "rb+") as f:  # on the disk before it takes the name: a power cut leaves old or new, not half
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
