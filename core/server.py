"""Local HTTP server for the interface.

Serves the web UI, page images (JPEG, rendered at the exact pixel width the viewer asks
for), per-page overlay data and the JSON API. Page resources are addressed by page
revision, so their responses are cacheable forever.

It listens on 127.0.0.1 on a random port. Every path starts with a secret token, the Host
header must name that address, and the API only accepts JSON POSTs from the same origin,
so neither web pages nor other local programs can drive it.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import annots as an
from . import pdftools as pt
from . import settings

log = logging.getLogger("aurora.server")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}
IMMUTABLE = "public, max-age=31536000, immutable"
MAX_BODY = 64 * 1024 * 1024


class _LRU:
    """Byte-bounded cache for rendered pages and overlay data."""

    def __init__(self, max_bytes: int):
        self.max = max_bytes
        self.size = 0
        self.items: collections.OrderedDict = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            val = self.items.get(key)
            if val is not None:
                self.items.move_to_end(key)
            return val

    def put(self, key, val: bytes) -> None:
        with self.lock:
            if key in self.items:
                return
            self.items[key] = val
            self.size += len(val)
            while self.size > self.max and self.items:
                _, old = self.items.popitem(last=False)
                self.size -= len(old)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):  # aborted image loads are normal while scrolling
            log.debug("connection closed by %s", client_address)
        else:  # a page that cannot be drawn, a bug: the log is the only trace under pythonw
            log.exception("request from %s failed", client_address)


class AppServer:
    def __init__(self, api, session, web_dir):
        self.api = api
        self.session = session
        self.web_dir = Path(web_dir).resolve()
        self.token = secrets.token_urlsafe(18)
        self.httpd = _Server(("127.0.0.1", 0), _Handler)
        self.httpd.app = self
        self.port = self.httpd.server_address[1]
        self.images = _LRU(160 * 1024 * 1024)
        self.infos = _LRU(32 * 1024 * 1024)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/"

    def start(self) -> "AppServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="aurora-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- dynamic resources ---------------------------------------------------------

    def page_jpeg(self, rev: int, width: int) -> bytes | None:
        key = (rev, width)
        data = self.images.get(key)
        if data is None:
            with self.session.lock:
                idx = self.session.page_of_rev(rev)
                if idx is None:
                    return None
                pix = pt.render_pixmap(self.session.doc[idx], width)
            data = pt.encode_jpeg(pix)  # outside the lock: other pages can render meanwhile
            self.images.put(key, data)
        return data

    def page_info(self, rev: int) -> bytes | None:
        data = self.infos.get(rev)
        if data is None:
            with self.session.lock:
                idx = self.session.page_of_rev(rev)
                if idx is None:
                    return None
                info = an.page_info(self.session.doc[idx])
            data = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.infos.put(rev, data)
        return data


# The pages load nothing but the app's own files and API: a file that slips markup into the interface (a
# comment, a file name) cannot run script or send anything elsewhere. The interface's own markup sets style
# attributes (colours, widths), hence 'unsafe-inline' for styles only.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
       "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

PRINT_STYLE = """
@page{margin:0}html,body{margin:0;background:#fff}
.p{break-after:page;page-break-after:always;height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
.p:last-child{break-after:auto;page-break-after:auto}
img{max-width:100%;max-height:100vh;object-fit:contain;display:block}
"""
PRINT_SCRIPT = """
var imgs=[].slice.call(document.images),left=imgs.length;
function done(){if(--left<=0){parent.postMessage({auroraPrintReady:true},location.origin);}}
if(!left)done();imgs.forEach(function(i){if(i.complete)done();else{i.onload=done;i.onerror=done;}});
"""
PRINT_HTML = (f'<!doctype html><html><head><meta charset="utf-8"><title>Print</title><style>{PRINT_STYLE}</style>'
              f"</head><body>__PAGES__<script>{PRINT_SCRIPT}</script></body></html>")


def _sha(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


# the print page is framed by the interface and runs only its own style and script, named by their hash
PRINT_CSP = (f"default-src 'none'; script-src {_sha(PRINT_SCRIPT)}; style-src {_sha(PRINT_STYLE)}; img-src 'self'; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Aurora"

    def log_message(self, fmt, *args):  # quiet: the UI makes many requests
        log.debug("%s - %s", self.address_string(), fmt % args)

    @property
    def app(self) -> AppServer:
        return self.server.app  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              cache: str = "no-store", headers: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")  # every address holds the secret token
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _route(self) -> str | None:
        """Path after the token, or None when the request is not for us."""
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"):
            return None
        parts = self.path.split("/", 2)
        if len(parts) < 2 or not secrets.compare_digest(parts[1], self.app.token):
            return None
        return parts[2] if len(parts) > 2 else ""

    def do_GET(self):
        rest = self._route()
        if rest is None:
            return self._send(404)
        url = urlsplit(rest)
        path = unquote(url.path)
        m = re.fullmatch(r"page/(\d+)/(\d+)\.jpg", path)
        if m:
            width = max(16, min(int(m.group(2)), 8000))
            data = self.app.page_jpeg(int(m.group(1)), width)
            return self._send(200, data, "image/jpeg", IMMUTABLE) if data else self._send(404)
        m = re.fullmatch(r"info/(\d+)\.json", path)
        if m:
            data = self.app.page_info(int(m.group(1)))
            return self._send(200, data, "application/json", IMMUTABLE) if data else self._send(404)
        m = re.fullmatch(r"sig/([0-9a-f]{12})\.png", path)
        if m:
            try:
                return self._send(200, settings.signature_bytes(m.group(1)), "image/png", "no-cache")
            except (OSError, ValueError):
                return self._send(404)
        if path == "print":
            q = parse_qs(url.query)
            width = max(200, min(int((q.get("w") or ["1400"])[0]), 4000))
            revs = [int(r) for r in (q.get("revs") or [""])[0].split(",") if r.isdigit()]
            pages = "".join(f'<div class="p"><img src="page/{r}/{width}.jpg"></div>' for r in revs)
            return self._send(200, PRINT_HTML.replace("__PAGES__", pages).encode(), "text/html; charset=utf-8",
                              headers={"Content-Security-Policy": PRINT_CSP})
        return self._static(path or "index.html")

    def _static(self, rel: str):
        try:
            target = (self.app.web_dir / rel).resolve()
            target.relative_to(self.app.web_dir)
        except (ValueError, OSError):
            return self._send(404)
        if not target.is_file():
            return self._send(404)
        st = target.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        mime = MIME.get(target.suffix.lower(), "application/octet-stream")
        headers = {"ETag": etag, **({"Content-Security-Policy": CSP} if target.suffix.lower() == ".html" else {})}
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", mime, "no-cache", headers)
        return self._send(200, target.read_bytes(), mime, "no-cache", headers)

    def do_POST(self):
        rest = self._route()
        if rest is None:
            return self._send(404)
        m = re.fullmatch(r"api/([a-z_]+)", rest)
        if not m:
            return self._send(404)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.app.port}", f"http://localhost:{self.app.port}"):
            return self._send(403)
        if not (self.headers.get("Content-Type", "").startswith("application/json")):
            return self._send(415)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._send(400)
        result = self.app.api.dispatch(m.group(1), payload if isinstance(payload, dict) else {})
        body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        return self._send(200, body, "application/json; charset=utf-8")
