"""install.exe / uninstall.exe logic (scripts/install.py) that does not need a real installation."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the installer is Windows only")


def load_installer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("aurora_install", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_half_removed_program_folder_is_still_ours(tmp_path):
    ins = load_installer()
    assert ins.ours(tmp_path)  # empty
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "locked.dll").write_bytes(b"x")
    assert ins.ours(tmp_path)  # a removal that a locked file stopped halfway
    left = tmp_path / "left"
    left.mkdir()
    (left / "uninstall.exe").write_bytes(b"MZ")
    assert ins.ours(left)  # only the locked uninstall.exe is left
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("someone else's folder")
    assert not ins.ours(other)
    (other / "Aurora PDF.exe").write_bytes(b"MZ")
    assert ins.ours(other)
