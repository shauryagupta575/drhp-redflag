"""Tables from financial-statement pages with pdfplumber (build guide Phase 2 step 3).

Each page's tables are stored as JSON: a list of tables, each a list of rows, each a list
of cell strings (None for empty cells becomes "").
"""

from collections.abc import Iterable
from pathlib import Path

import pdfplumber


def extract_tables(path: Path, page_numbers: Iterable[int]) -> dict[int, list[list[list[str]]]]:
    """{1-based page number: tables} for the requested pages that contain tables."""
    out: dict[int, list[list[list[str]]]] = {}
    with pdfplumber.open(path) as pdf:
        for page_no in sorted(set(page_numbers)):
            if not 1 <= page_no <= len(pdf.pages):
                continue
            page = pdf.pages[page_no - 1]
            tables = [
                [[(cell or "").strip() for cell in row] for row in table]
                for table in page.extract_tables()
                if table
            ]
            if tables:
                out[page_no] = tables
            page.flush_cache()  # keep memory flat on long statements
    return out
