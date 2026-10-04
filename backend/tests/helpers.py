import pymupdf


def make_pdf(pages: int) -> bytes:
    """A valid PDF with `pages` blank pages."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data: bytes = doc.tobytes()
    doc.close()
    return data
