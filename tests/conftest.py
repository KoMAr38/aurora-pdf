"""Shared test setup: import path, a private data folder, sample documents."""
import io
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# keep tests out of the real user profile (settings, IDs, signatures)
os.environ.setdefault("AURORA_DATA_DIR", tempfile.mkdtemp(prefix="aurora_data_"))

import pymupdf  # noqa: E402

FONT = os.path.join(ROOT, "resources", "fonts", "NotoSans-Regular.ttf")


def make_doc(pages: int = 3, rotation: int = 0, width: float = 420, height: float = 595) -> pymupdf.Document:
    """A small document whose text reads upright on screen, whatever the /Rotate."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.set_rotation(rotation)
        lines = [(f"Aurora sample page {i + 1}", 18), (f"marker-{i + 1} lorem ipsum dolor", 12),
                 ("Čačak Šabac Đurđevdan", 12)]
        y = 80
        for text, size in lines:
            pt = pymupdf.Point(50, y) * page.derotation_matrix
            page.insert_text(pt, text, fontsize=size, fontname="noto", fontfile=FONT, rotate=page.rotation)
            y += 40
    return doc


def save_tmp(doc: pymupdf.Document, folder, name: str) -> str:
    path = os.path.join(str(folder), name)
    doc.save(path)
    return path


def png_bytes(w=200, h=100, color=(220, 30, 30, 255), mark=True) -> bytes:
    """RGBA PNG; with ``mark`` a blue block sits in the top-left corner (orientation check)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (w, h), color)
    if mark:
        ImageDraw.Draw(img).rectangle([0, 0, w // 4, h // 3], fill=(20, 40, 230, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def words_display(page):
    """(rect_in_display_space, text) for every word on the page."""
    return [(pymupdf.Rect(w[:4]) * page.rotation_matrix, w[4]) for w in page.get_text("words")]


def line_angles(page):
    """Visual angle (degrees, counter-clockwise on screen) of each text line."""
    out = []
    m = page.rotation_matrix
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            v = pymupdf.Point(ln["dir"]) * m - pymupdf.Point(0, 0) * m
            out.append(round(-math.degrees(math.atan2(v.y, v.x))) % 360)
    return out
