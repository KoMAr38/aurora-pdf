"""Text recognition for scanned pages, using MuPDF's built-in Tesseract and the
language files bundled in resources/tessdata (no system Tesseract needed).

A recognised page gets an invisible text layer: MuPDF's own PDF-OCR writer produces a
page with the scan plus glyph-less text placed exactly over the words; we drop its copy
of the image and lay only that text over the original page. Text then becomes
searchable, selectable and copyable here and in every other PDF reader.
"""
from __future__ import annotations

import re

import pymupdf

from .paths import TESSDATA

LANGUAGES = {
    "srp_latn": "Srpski (latinica)",
    "srp": "Српски (ћирилица)",
    "nor": "Norsk",
    "eng": "English",
}

_DO_OPERATOR = re.compile(rb"/[A-Za-z0-9_.]+\s+Do\b")


def available_languages() -> dict[str, str]:
    return {code: name for code, name in LANGUAGES.items() if (TESSDATA / f"{code}.traineddata").exists()}


def page_has_text(page: pymupdf.Page, min_chars: int = 20) -> bool:
    """Text in the page itself: get_text() also reads the comments and form fields on it, which a scan can carry.
    DisplayList.get_textpage hands back MuPDF's own object (PyMuPDF 1.28), hence the TextPage around it."""
    text = pymupdf.TextPage(page.get_displaylist(annots=False).get_textpage()).extractText()
    return len(text.strip()) >= min_chars


def looks_scanned(doc: pymupdf.Document, sample: int = 8) -> bool:
    """True when the first pages carry images but (almost) no text."""
    n = min(len(doc), sample)
    if n == 0:
        return False
    scanned = sum(1 for i in range(n) if doc[i].get_images() and not page_has_text(doc[i]))
    return scanned * 2 > n


def ocr_page(page: pymupdf.Page, languages: str = "eng", dpi: int = 300) -> int:
    """Recognise one page and add an invisible text layer. Returns the word count. ``languages``: codes
    from LANGUAGES joined by "+", as ocr_start checks them."""
    pix = page.get_pixmap(dpi=dpi, annots=False)  # upright, as the user sees it, without comments and fields
    data = pix.pdfocr_tobytes(compress=True, language=languages, tessdata=str(TESSDATA))
    ocr = pymupdf.open("pdf", data)
    try:
        opage = ocr[0]
        for xref in opage.get_contents():
            ocr.update_stream(xref, _DO_OPERATOR.sub(b"", ocr.xref_stream(xref)))
        opage.clean_contents()  # drops the picture from the resources too, else every page keeps a copy
        words = len(opage.get_text("words"))
        if words:
            target = page.rect * page.derotation_matrix
            target.normalize()
            # rotate=page.rotation maps the upright OCR page back onto the page's own axes
            page.show_pdf_page(target, ocr, 0, rotate=page.rotation, overlay=True)
        return words
    finally:
        ocr.close()
