"""Phase 2 pipeline against a real Postgres + pgvector, with a fake embedding model."""

import hashlib
import math
import re
from datetime import date
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import EMBEDDING_DIM, Chunk, Company, Document, Ipo, Page
from app.parsing import embeddings
from app.parsing import run as parsing_run

TOC = [
    "SECTION I: GENERAL ..... 1",
    "SECTION II: RISK FACTORS ..... 2",
    "CAPITAL STRUCTURE ..... 3",
    "OBJECTS OF THE OFFER ..... 4",
    "BASIS FOR OFFER PRICE ..... 5",
    "OUR PROMOTERS AND PROMOTER GROUP ..... 6",
    "OUR GROUP COMPANIES ..... 7",
    "RESTATED FINANCIAL INFORMATION ..... 8",
    "FINANCIAL INDEBTEDNESS ..... 10",
    "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS ..... 11",
    "GOVERNMENT AND OTHER APPROVALS ..... 12",
]
BODY = [  # printed pages 1..12
    ["DEFINITIONS AND ABBREVIATIONS"],
    ["RISK FACTORS", "internal risks to our business and results of operations"],
    ["CAPITAL STRUCTURE", "authorised issued and paid-up share capital, pledge of shares"],
    ["OBJECTS OF THE OFFER", "utilisation of net proceeds and repayment of borrowings"],
    ["BASIS FOR OFFER PRICE", "earnings per share and price to earnings ratio"],
    ["OUR PROMOTERS AND PROMOTER GROUP", "details of promoters"],
    ["OUR GROUP COMPANIES", "details of group companies"],
    ["RESTATED FINANCIAL INFORMATION", "restated statement of assets and liabilities"],
    ["Notes forming part of the restated financial information",
     "Note 30: Related party disclosures", "names of related parties and transactions"],
    ["FINANCIAL INDEBTEDNESS", "secured and unsecured borrowings"],
    ["OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS", "criminal proceedings"],
    ["GOVERNMENT AND OTHER APPROVALS", "licences"],
]  # fmt: skip


def make_offer_pdf(path: Path) -> None:
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_text((72, 80), "DRAFT RED HERRING PROSPECTUS - please read the contents")
    toc = doc.new_page()
    toc.insert_text((72, 60), "TABLE OF CONTENTS")
    for i, line in enumerate(TOC):
        toc.insert_text((72, 90 + 18 * i), line, fontsize=9)
    for printed, lines in enumerate(BODY, start=1):
        page = doc.new_page(width=595, height=842)
        for j, line in enumerate(lines):
            page.insert_text((72, 80 + 20 * j), line, fontsize=11)
        page.insert_text((290, 820), str(printed), fontsize=9)
        if printed == 8:  # a ruled table on the restated financials page
            for x in (72, 272, 472):
                page.draw_line((x, 300), (x, 360))
            for y in (300, 330, 360):
                page.draw_line((72, y), (472, y))
            page.insert_text((80, 320), "Revenue", fontsize=10)
            page.insert_text((280, 320), "100.0", fontsize=10)
            page.insert_text((80, 350), "PAT", fontsize=10)
            page.insert_text((280, 350), "12.5", fontsize=10)
    doc.save(path)
    doc.close()


def fake_vector(text: str) -> list[float]:
    """Hashed bag of words, normalised: similar words give similar vectors."""
    vec = [0.0] * EMBEDDING_DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % EMBEDDING_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        embeddings,
        "token_offsets",
        lambda text: [(m.start(), m.end()) for m in re.finditer(r"\S+", text)],
    )
    monkeypatch.setattr(embeddings, "embed_passages", lambda texts: [fake_vector(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", fake_vector)


@pytest.fixture
def document(db_session: Session, tmp_path: Path) -> Document:
    (tmp_path / "pdfs").mkdir()
    make_offer_pdf(tmp_path / "pdfs" / "offer.pdf")
    company = Company(name="Example Limited", nse_symbol="EXAMPLE")
    db_session.add(company)
    db_session.flush()
    ipo = Ipo(company_id=company.id, open_date=date(2025, 1, 1), exchange="NSE")
    db_session.add(ipo)
    db_session.flush()
    doc = Document(
        ipo_id=ipo.id,
        doc_type="DRHP",
        source_url="https://example.test/offer.pdf",
        sha256="0" * 64,
        n_pages=14,
        storage_path="pdfs/offer.pdf",
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def counts(session: Session, doc_id: int) -> tuple[int, int]:
    pages = session.scalar(select(func.count()).where(Page.document_id == doc_id))
    chunks = session.scalar(select(func.count()).where(Chunk.document_id == doc_id))
    return pages or 0, chunks or 0


def test_pipeline_stores_pages_chunks_sections_and_tables(
    db_session: Session, document: Document, tmp_path: Path
) -> None:
    outcome = parsing_run.process_document(db_session, document, tmp_path)
    assert outcome.n_pages == 14 and outcome.scanned_pages == 0
    n_pages, n_chunks = counts(db_session, document.id)
    assert n_pages == 14 and n_chunks == outcome.n_chunks >= 1

    db_session.refresh(document)
    sm = document.section_map
    assert sm is not None
    assert sm["risk_factors"] == [4, 4]
    assert sm["capital_structure"] == [5, 5]
    assert sm["restated_financials"] == [10, 11]
    assert sm["related_party"] == [11, 11]
    assert sm["financial_indebtedness"] == [12, 12]
    assert sm["outstanding_litigation"] == [13, 13]
    assert outcome.result.missing == []

    page10 = db_session.scalars(
        select(Page).where(Page.document_id == document.id, Page.page_no == 10)
    ).one()
    assert page10.tables == [[["Revenue", "100.0"], ["PAT", "12.5"]]]
    assert page10.blocks and all(len(b) == 5 for b in page10.blocks)
    other = db_session.scalars(
        select(Page).where(Page.document_id == document.id, Page.page_no == 4)
    ).one()
    assert other.tables is None  # tables only from financial-statement pages


def test_pgvector_search_ranks_by_similarity(
    db_session: Session, document: Document, tmp_path: Path
) -> None:
    parsing_run.process_document(db_session, document, tmp_path)
    search = parsing_run.pgvector_search(db_session, document.id)
    hits = search("criminal proceedings", 3)
    assert len(hits) <= 3
    assert hits == sorted(hits, key=lambda h: -h[2])
    lo, hi, _ = hits[0]
    assert lo <= 13 <= hi  # the litigation page


def test_rerun_skips_cached_stages_and_force_rebuilds(
    db_session: Session, document: Document, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsing_run.process_document(db_session, document, tmp_path)
    first_ids = set(db_session.scalars(select(Chunk.id).where(Chunk.document_id == document.id)))

    calls: list[int] = []

    def counting_embed(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_passages", counting_embed)
    parsing_run.process_document(db_session, document, tmp_path)
    assert calls == []  # chunks were cached
    assert (
        set(db_session.scalars(select(Chunk.id).where(Chunk.document_id == document.id)))
        == first_ids
    )

    parsing_run.process_document(db_session, document, tmp_path, force=True)
    assert calls  # rebuilt
    new_ids = set(db_session.scalars(select(Chunk.id).where(Chunk.document_id == document.id)))
    assert new_ids and not new_ids & first_ids
    assert counts(db_session, document.id)[0] == 14


def test_spot_check_sheet(db_session: Session, document: Document, tmp_path: Path) -> None:
    outcome = parsing_run.process_document(db_session, document, tmp_path)
    sheet = parsing_run.write_spot_check(db_session, [outcome], tmp_path).read_text()
    assert "## Example Limited (DRHP" in sheet
    assert "| risk_factors | 4-4 | toc | yes | yes |" in sheet
    assert "Missing: none" in sheet
