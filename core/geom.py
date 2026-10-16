"""Coordinate and colour helpers shared by the engine modules.

Two coordinate spaces matter:

* display space  - what the user sees: ``page.rect`` with the page /Rotate applied,
  origin top-left, units are PDF points. Everything the UI sends and receives is here.
* unrotated space - PyMuPDF page coordinates: the page as it would show at /Rotate 0. ``get_text``,
  ``search_for`` and every ``add_*_annot`` call work here; :func:`turn` maps it to display space.

PyMuPDF reads an annotation's rect through ``page.rotation_matrix``, puts a picture or a page with
``~page.transformation_matrix`` and text with the CropBox position: on most pages these agree, but not on a
turned page whose CropBox starts away from 0,0, reaches past the MediaBox, or that has a /UserUnit. So each
kind of call gets its own matrix here, all made from MuPDF's own page transform (:func:`page_ctm`).
"""
from __future__ import annotations

import math
from typing import Sequence

import pymupdf


def page_ctm(page: pymupdf.Page) -> pymupdf.Matrix:
    """PDF user space -> display space, as MuPDF draws the page: CropBox clipped to the MediaBox, /UserUnit, /Rotate.
    shortcut: goes through MuPDF's own objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    m = pymupdf.mupdf
    ctm = m.FzMatrix()
    m.pdf_page_transform(pymupdf._as_pdf_page(page), m.FzRect(), ctm)
    return pymupdf.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)


def turn(page: pymupdf.Page) -> pymupdf.Matrix:
    """Unrotated space -> display space: the page turned about its real size. page.rotation_matrix turns it about
    the CropBox as written, which is another size when MuPDF clips the CropBox or scales by /UserUnit."""
    w, h = page.rect.width, page.rect.height
    return {90: pymupdf.Matrix(0, 1, -1, 0, w, 0), 180: pymupdf.Matrix(-1, 0, 0, -1, w, h),
            270: pymupdf.Matrix(0, -1, 1, 0, 0, h)}.get(page.rotation, pymupdf.Matrix(1, 0, 0, 1, 0, 0))


def rect_to_unrot(r: Sequence[float], page: pymupdf.Page) -> pymupdf.Rect:
    """Display rect [x0,y0,x1,y1] -> normalized rect in unrotated page space (for the add_*_annot calls)."""
    out = pymupdf.Rect(r) * ~turn(page)
    out.normalize()
    return out


def point_to_unrot(p: Sequence[float], page: pymupdf.Page) -> pymupdf.Point:
    return pymupdf.Point(p) * ~turn(page)


def vec_to_unrot(dx: float, dy: float, page: pymupdf.Page) -> pymupdf.Point:
    """A displacement (no translation part) from display to unrotated space."""
    m = ~turn(page)
    return pymupdf.Point(dx, dy) * m - pymupdf.Point(0, 0) * m


def to_pdf(page: pymupdf.Page) -> pymupdf.Matrix:
    """The space PyMuPDF reads annotation points in (page.rotation_matrix turns it to display space) -> PDF
    user space: so a moved drawing lands where it was dragged on every page."""
    return ~(page_ctm(page) * page.derotation_matrix)


def to_insert(page: pymupdf.Page) -> pymupdf.Matrix:
    """Display space -> the space insert_image and show_pdf_page take, which they turn into PDF user space with
    ~page.transformation_matrix (that leaves out the CropBox position of a turned page)."""
    return ~page_ctm(page) * page.transformation_matrix


def to_text(page: pymupdf.Page) -> tuple[pymupdf.Matrix, float]:
    """Display space -> the point insert_text takes (it adds the CropBox position as written and turns y over at
    the MediaBox height), and the factor that turns a size on screen into the size to write (/UserUnit)."""
    cb, mb = page.cropbox_position, page.mediabox_size
    return ~page_ctm(page) * ~pymupdf.Matrix(1, 0, 0, -1, cb.x, mb.y - cb.y), size_unit(page)


def size_unit(page: pymupdf.Page) -> float:
    """The factor that turns a size on screen (font size, line width) into the size to write: 1 / the /UserUnit."""
    ctm = page_ctm(page)
    return 1 / math.sqrt(abs(ctm.a * ctm.d - ctm.b * ctm.c))


def display_angle(direction: Sequence[float], page: pymupdf.Page) -> int:
    """Text direction (unit vector, unrotated space) -> on-screen angle snapped to 0/90/180/270.

    Angles are clockwise, matching CSS ``rotate()`` with y pointing down.
    """
    m = page.rotation_matrix
    v = pymupdf.Point(direction) * m - pymupdf.Point(0, 0) * m
    deg = math.degrees(math.atan2(v.y, v.x))
    return int(round(deg / 90.0)) * 90 % 360


def rounded(seq, nd: int = 2) -> list[float]:
    return [round(float(v), nd) for v in seq]


def rgb(color: str | Sequence[float] | None, default=(0, 0, 0)) -> tuple[float, float, float]:
    """'#rrggbb' (or an rgb tuple in 0..1) -> rgb tuple in 0..1."""
    if color is None or color == "":
        return tuple(default)  # type: ignore[return-value]
    if isinstance(color, str):
        c = color.strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) != 6:
            raise ValueError(f"bad colour: {color}")
        return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    return tuple(float(v) for v in color[:3])  # type: ignore[return-value]


def hexcolor(c: Sequence[float] | None) -> str | None:
    if not c:
        return None
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c[:3])
