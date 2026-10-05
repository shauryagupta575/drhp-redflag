import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from app.parsing.chunking import chunk_pages
from app.parsing.pdf_text import MIN_TEXT_CHARS, extract_pages
from app.parsing.tables import extract_tables


@dataclass
class T:
    page_no: int
    text: str


def word_offsets(text: str) -> list[tuple[int, int]]:
    """A stand-in tokenizer: one token per word."""
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]


def words(n: int, prefix: str) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_chunks_have_fixed_size_overlap_and_page_spans() -> None:
    pages = [T(1, words(30, "a")), T(2, words(30, "b")), T(3, words(15, "c"))]
    chunks = chunk_pages(pages, word_offsets, chunk_tokens=20, overlap_tokens=5)
    sizes = [len(word_offsets(c.text)) for c in chunks]
    assert sizes[:-1] == [20] * (len(chunks) - 1) and sizes[-1] <= 20
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)
    assert (chunks[1].page_start, chunks[1].page_end) == (1, 2)  # spans a page break
    # 5-token overlap: the last 5 words of one chunk start the next.
    assert (
        word_offsets(chunks[0].text) and chunks[0].text.split()[-5:] == chunks[1].text.split()[:5]
    )
    # Every word of the document is in some chunk; chunk text is original page text.
    covered = {w for c in chunks for w in c.text.split()}
    assert covered == set(" ".join(p.text for p in pages).split())
    assert chunks[-1].page_end == 3


def test_short_document_is_one_chunk_and_empty_document_none() -> None:
    chunks = chunk_pages([T(1, "only a few words")], word_offsets, chunk_tokens=500)
    assert [(c.page_start, c.page_end, c.text) for c in chunks] == [(1, 1, "only a few words")]
    assert chunk_pages([T(1, "")], word_offsets) == []


def test_overlap_must_be_smaller_than_chunk() -> None:
    with pytest.raises(ValueError):
        chunk_pages([T(1, "a b")], word_offsets, chunk_tokens=10, overlap_tokens=10)


def make_doc(path: Path, pages: list[str | None]) -> None:
    """None makes a page with only a drawing (no text layer)."""
    doc = pymupdf.open()
    for i, text in enumerate(pages):
        page = doc.new_page(width=595, height=842)
        if text is None:
            page.draw_rect(pymupdf.Rect(100, 100, 300, 300), fill=(0.5, 0.5, 0.5))
            continue
        page.insert_text((72, 80), text, fontsize=11)
        page.insert_text((290, 820), str(i + 1 - 1), fontsize=9)  # footer page number
    doc.save(path)
    doc.close()


def test_extract_pages_text_blocks_footer_numbers_and_ocr_fallback(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    make_doc(pdf, ["Cover of the document with enough text", "RISK FACTORS heading here", None])
    calls: list[int] = []

    def fake_ocr(page: Any) -> tuple[str, list[list[Any]]]:
        calls.append(page.number + 1)
        return "OCR TEXT", [[1.0, 2.0, 3.0, 4.0, "OCR TEXT"]]

    pages = extract_pages(pdf, ocr=fake_ocr)
    assert [p.page_no for p in pages] == [1, 2, 3]
    assert "RISK FACTORS" in pages[1].text
    assert not pages[1].is_scanned
    assert pages[1].printed_no == 1  # footer "1" on the second PDF page
    x0, y0, x1, y1, text = next(b for b in pages[1].blocks if "RISK" in b[4])
    assert 60 < x0 < x1 and y0 < y1  # coordinates kept for highlighting
    assert pages[2].is_scanned and pages[2].text == "OCR TEXT" and calls == [3]
    assert pages[2].printed_no is None
    assert MIN_TEXT_CHARS > 0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_real_tesseract_ocr_reads_rendered_text(tmp_path: Path) -> None:
    from app.parsing.ocr import ocr_page

    src = pymupdf.open()
    page = src.new_page(width=595, height=300)
    page.insert_text((60, 150), "RELATED PARTY TRANSACTIONS", fontsize=28)
    pix = page.get_pixmap(dpi=150)
    scanned = pymupdf.open()
    img_page = scanned.new_page(width=595, height=300)
    img_page.insert_image(img_page.rect, pixmap=pix)  # image only: no text layer
    assert img_page.get_text().strip() == ""
    text, blocks = ocr_page(img_page)
    assert "RELATED PARTY" in text.upper()
    assert blocks and len(blocks[0]) == 5


def test_extract_tables_from_ruled_table(tmp_path: Path) -> None:
    pdf = tmp_path / "table.pdf"
    doc = pymupdf.open()
    doc.new_page()  # page 1: no table
    page = doc.new_page(width=595, height=842)
    xs, ys = [72, 272, 472], [100, 130, 160, 190]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    cells = [["Particulars", "FY2025"], ["Revenue", "1,234.5"], ["PAT", "(42.18)"]]
    for r, row in enumerate(cells):
        for c, value in enumerate(row):
            page.insert_text((xs[c] + 5, ys[r] + 20), value, fontsize=10)
    doc.save(pdf)
    doc.close()
    tables = extract_tables(pdf, [1, 2, 99])
    assert list(tables) == [2]
    assert tables[2] == [cells]
