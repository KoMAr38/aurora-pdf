"""The update check against GitHub Releases, with GitHub's answers played back (no network)."""
import io
import json
import urllib.error

import pytest

from api import Api, Dialogs
from core import update
from core.paths import VERSION
from core.session import Session


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release(tag, assets=("install.exe", "uninstall.exe")):
    return {"tag_name": tag, "assets": [{"name": n, "size": 7, "browser_download_url": update.DOWNLOADS + f"{tag}/{n}"}
                                        for n in assets]}


def answer(monkeypatch, body=None, error=None):
    def urlopen(req, timeout=None):
        if error is not None:
            raise error
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())
    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)


def bump(version: str, k: int) -> str:
    parts = [int(p) for p in version.split(".")]
    parts[k] += 1
    return ".".join(map(str, parts))


def test_a_newer_release_is_offered_and_an_older_one_is_not(monkeypatch):
    answer(monkeypatch, release("v" + bump(VERSION, 2)))
    info = update.latest()
    assert info["newer"] and info["url"].endswith("/install.exe") and info["version"] == bump(VERSION, 2)
    answer(monkeypatch, release("v" + VERSION))
    assert not update.latest()["newer"]
    answer(monkeypatch, release("v" + bump(VERSION, 0), assets=("uninstall.exe",)))  # no installer in it
    assert not update.latest()["newer"]


def test_no_release_yet_is_up_to_date_and_offline_is_a_code(monkeypatch):
    answer(monkeypatch, error=urllib.error.HTTPError(update.LATEST, 404, "Not Found", {}, None))
    assert update.latest() == {"version": VERSION, "newer": False, "url": "", "size": 0}
    answer(monkeypatch, error=urllib.error.URLError("no network"))
    api = Api(Session(), Dialogs(), test_mode=True)
    assert api.dispatch("update_check", {})["error"] == "UPDATE_OFFLINE"


def test_only_this_projects_release_files_are_downloaded(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        update.download("https://example.com/install.exe")
    answer(monkeypatch, b"MZ installer")
    path = update.download(update.DOWNLOADS + "v9.9.9/install.exe")
    assert open(path, "rb").read() == b"MZ installer" and path.endswith("install.exe")


def test_the_store_copy_never_updates_itself(monkeypatch):
    api = Api(Session(), Dialogs(), test_mode=True)
    api.packaged = True
    assert api.dispatch("state", {})["packaged"] is True
    assert api.dispatch("update_start", {})["error"] == "NOT_ALLOWED"


def test_the_program_asks_github_by_itself_only_in_the_copy_from_install_exe():
    assert Api(Session(), Dialogs()).dispatch("state", {})["updateCheck"] is True
    store = Api(Session(), Dialogs())
    store.packaged = True
    assert store.dispatch("state", {})["updateCheck"] is False
    assert Api(Session(), Dialogs(), test_mode=True).dispatch("state", {})["updateCheck"] is False  # tests stay offline
