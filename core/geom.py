"""Coordinate and colour helpers shared by the engine modules.

Two coordinate spaces matter:

* display space  - what the user sees: ``page.rect`` with the page /Rotate applied,
  origin top-left, units are PDF points. Everything the UI sends and receives is here.
* unrotated space - PyMuPDF page coordinates. ``get_text``, ``search_for`` and every
  ``add_*_annot`` / ``insert_*`` call work here.

``page.rotation_matrix`` maps unrotated -> display, ``page.derotation_matrix`` the reverse.
"""
from __future__ import annotations

import math
from typing import Sequence

import pymupdf


def rect_to_unrot(r: Sequence[float], page: pymupdf.Page) -> pymupdf.Rect:
    """Display rect [x0,y0,x1,y1] -> normalized rect in unrotated page space."""
    out = pymupdf.Rect(r) * page.derotation_matrix
    out.normalize()
    return out


def point_to_unrot(p: Sequence[float], page: pymupdf.Page) -> pymupdf.Point:
    return pymupdf.Point(p) * page.derotation_matrix


def vec_to_unrot(dx: float, dy: float, page: pymupdf.Page) -> pymupdf.Point:
    """A displacement (no translation part) from display to unrotated space."""
    m = page.derotation_matrix
    return pymupdf.Point(dx, dy) * m - pymupdf.Point(0, 0) * m


def to_pdf(page: pymupdf.Page) -> pymupdf.Matrix:
    """Unrotated space -> PDF user space: the inverse of how PyMuPDF reads annotation points (MuPDF's page
    transform, then derotated), so it holds for a CropBox past the MediaBox and for /UserUnit too.
    ~page.transformation_matrix leaves out the CropBox offset of a turned page (PyMuPDF 1.28): a signature on a
    cropped, turned page landed off the page, and moved drawings jumped.
    shortcut: goes through MuPDF's own objects, check again when requirements.txt moves past PyMuPDF 1.28."""
    m = pymupdf.mupdf
    ctm = m.FzMatrix()
    m.pdf_page_transform(pymupdf._as_pdf_page(page), m.FzRect(), ctm)
    return ~(pymupdf.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f) * page.derotation_matrix)


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
