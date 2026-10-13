"""Where bundled resources live and where per-user data is written."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """Project folder when run from source; the unpacked bundle when frozen."""
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else Path(__file__).resolve().parent.parent


def resource(*parts: str) -> Path:
    return app_root().joinpath(*parts)


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
