"""OCR fallback for pages without a text layer (build guide Phase 2 step 2): Tesseract,
through PyMuPDF's OCR text page so block coordinates are kept for highlighting."""

from typing import Any

import pymupdf

OCR_DPI = 300


def ocr_page(page: pymupdf.Page) -> tuple[str, list[list[Any]]]:
    """Return (text, blocks) for one page, where blocks are [x0, y0, x1, y1, text]."""
    textpage = page.get_textpage_ocr(dpi=OCR_DPI, full=True)
    text: str = page.get_text("text", textpage=textpage)
    raw = page.get_text("blocks", textpage=textpage)
    blocks = [
        [round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1), b[4]]
        for b in raw
        if b[6] == 0
    ]
    return text, blocks
