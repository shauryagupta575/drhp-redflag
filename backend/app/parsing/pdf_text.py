"""Page-level text extraction with PyMuPDF (build guide Phase 2 step 1).

Every page keeps its 1-based PDF page number, its text, and its text blocks with
coordinates ([x0, y0, x1, y1, text] in PDF points) for later citation highlighting.
Pages with (almost) no text layer are treated as scanned and sent to the OCR fallback.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from app.parsing.ocr import ocr_page

# A page with fewer extractable characters than this has no usable text layer.
MIN_TEXT_CHARS = 20

_PAGE_NUMBER = re.compile(r"^\d{1,4}$")


@dataclass
class PageContent:
    page_no: int  # 1-based PDF page number
    text: str
    blocks: list[list[Any]]
    is_scanned: bool
    printed_no: int | None  # page number printed in the footer, if any


def _printed_number(page: pymupdf.Page, blocks: list[list[Any]]) -> int | None:
    """The page number printed in the footer: a block that is a bare integer in the
    bottom 12% of the page."""
    bottom = page.rect.height * 0.88
    for _x0, y0, _x1, _y1, text in blocks:
        value = text.strip()
        if y0 >= bottom and _PAGE_NUMBER.match(value):
            return int(value)
    return None


def extract_pages(
    path: Path, ocr: Callable[[pymupdf.Page], tuple[str, list[list[Any]]]] = ocr_page
) -> list[PageContent]:
    pages = []
    with pymupdf.open(path) as doc:  # type: ignore[no-untyped-call]
        for index in range(doc.page_count):
            page = doc[index]
            text: str = page.get_text("text")
            raw = page.get_text("blocks")
            blocks = [
                [round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1), b[4]]
                for b in raw
                if b[6] == 0
            ]
            scanned = len(text.strip()) < MIN_TEXT_CHARS
            if scanned:
                text, blocks = ocr(page)
            pages.append(
                PageContent(
                    page_no=index + 1,
                    text=text,
                    blocks=blocks,
                    is_scanned=scanned,
                    printed_no=None if scanned else _printed_number(page, blocks),
                )
            )
    return pages
