"""Run the real app server (python main.py --serve) and drive its UI in Chromium.

Used by the end-to-end tests and handy on its own:  python tests/e2e/harness.py shot out.png
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def app_server(data_dir: str | None = None, extra_args: list[str] | None = None):
    """Start the app in test mode; yields (url, data_dir). A data folder of its own goes when it stops."""
    with tempfile.TemporaryDirectory(prefix="aurora_e2e_", ignore_cleanup_errors=True) as own:
        data_dir = data_dir or own
        port_file = os.path.join(data_dir, "url.txt")
        env = dict(os.environ, AURORA_DATA_DIR=data_dir, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--serve", port_file, *(extra_args or [])],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 20
            url = ""
            while time.time() < deadline:
                if os.path.exists(port_file):
                    url = Path(port_file).read_text(encoding="utf-8").strip()
                    if url:
                        break
                if proc.poll() is not None:
                    raise RuntimeError("server exited: " + proc.stdout.read().decode(errors="replace"))
                time.sleep(0.05)
            if not url:
                raise RuntimeError("server did not start")
            yield url, data_dir
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def launch_browser(pw):
    """Chromium for the tests. On Windows that is the installed Microsoft Edge, the same
    engine as WebView2, so nothing has to be downloaded. AURORA_BROWSER picks another
    channel; without a usable channel Playwright's own Chromium is used."""
    channel = os.environ.get("AURORA_BROWSER", "msedge" if sys.platform == "win32" else "")
    if channel:
        try:
            return pw.chromium.launch(channel=channel)
        except Exception:  # shortcut: channel missing -> fall back to the bundled browser
            pass
    return pw.chromium.launch()


@contextmanager
def browser_page(url: str, width: int = 1380, height: int = 860, scale: float = 1):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = launch_browser(pw)
        ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale, color_scheme="dark")
        page = ctx.new_page()
        page.errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: m.type in ("error", "warning") and page.errors.append(f"console.{m.type}: {m.text}"))
        page.goto(url)
        page.wait_for_selector("body.ready", timeout=15000)
        try:
            yield page
        finally:
            browser.close()


def rpc(page, method: str, payload: dict | None = None) -> dict:
    """Call the backend from inside the page, exactly like the UI does."""
    return page.evaluate("([m, p]) => Aurora.rpc(m, p)", [method, payload or {}])


def script_dialogs(page, answers: list) -> None:
    """Answers for the next native file dialogs (test mode only)."""
    assert rpc(page, "test_dialogs", {"answers": answers})["ok"]


def open_pdf(page, path: str) -> None:
    page.evaluate("(p) => Aurora.openPath(p, {skipGuard: true})", str(path))
    page.wait_for_function("Aurora.S.doc && Aurora.S.doc.open", timeout=15000)
    settle(page)


def settle(page, ms: int = 450) -> None:
    """Let pending edits, image loads and overlays finish."""
    page.wait_for_function("() => !document.querySelector('#busy.show')", timeout=20000)
    page.evaluate("() => new Promise(r => setTimeout(r, 0))")
    page.wait_for_timeout(ms)


if __name__ == "__main__":
    out = sys.argv[2] if len(sys.argv) > 2 else "shot.png"
    with app_server() as (url, _), browser_page(url) as page:
        if len(sys.argv) > 3:
            open_pdf(page, sys.argv[3])
        page.wait_for_timeout(600)
        page.screenshot(path=out)
        print(json.dumps({"url": url, "errors": page.errors}, indent=1))
