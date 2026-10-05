"""Phase 2 pipeline: page text, OCR fallback, chunks + embeddings, section map, tables.

    python -m app.parsing.run                     # every downloaded document
    python -m app.parsing.run --doc-type DRHP     # only DRHPs
    python -m app.parsing.run --document-ids 3,7 --force

Each stage writes to Postgres and is skipped when its output already exists (pages,
chunks), so re-runs only redo what is missing; --force rebuilds everything for the
selected documents. Writes a spot-check sheet to data/processed/section_check.md.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Chunk, Company, Document, Ipo, Page
from app.parsing import embeddings
from app.parsing.chunking import chunk_pages
from app.parsing.pdf_text import PageContent, extract_pages
from app.parsing.section_locator import (
    SECTION_KEYS,
    LocatorResult,
    SemanticSearch,
    locate_sections,
)
from app.parsing.tables import extract_tables

log = logging.getLogger("app.parsing")


@dataclass
class DocOutcome:
    document_id: int
    label: str
    n_pages: int
    scanned_pages: int
    n_chunks: int
    result: LocatorResult
    table_pages: int


def pgvector_search(session: Session, document_id: int) -> SemanticSearch:
    """Cosine search over one document's chunks in Postgres (pgvector)."""

    def search(query: str, k: int) -> list[tuple[int, int, float]]:
        vector = embeddings.embed_query(query)
        # Exact ordering even with the HNSW index plus a document filter.
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        distance = Chunk.embedding.cosine_distance(vector)
        rows = session.execute(
            select(Chunk.page_start, Chunk.page_end, (1 - distance).label("sim"))
            .where(Chunk.document_id == document_id)
            .order_by(distance)
            .limit(k)
        ).all()
        return [(int(a), int(b), float(s)) for a, b, s in rows]

    return search


def _stored_scanned_text(session: Session, document_id: int) -> dict[int, tuple[str, list[Any]]]:
    rows = session.execute(
        select(Page.page_no, Page.text, Page.blocks).where(
            Page.document_id == document_id, Page.is_scanned.is_(True)
        )
    ).all()
    return {n: (t, b or []) for n, t, b in rows}


def process_document(
    session: Session, doc: Document, data_dir: Path, force: bool = False
) -> DocOutcome:
    path = data_dir / doc.storage_path
    if force:
        session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        session.execute(delete(Page).where(Page.document_id == doc.id))
        session.commit()

    # 1-2. Page text (+ OCR). Pages OCR'd on an earlier run are not OCR'd again.
    have_pages = session.scalar(select(func.count()).where(Page.document_id == doc.id)) or 0
    previous = _stored_scanned_text(session, doc.id)
    from app.parsing.ocr import ocr_page

    def ocr(page: pymupdf.Page) -> tuple[str, list[Any]]:
        number = page.number + 1 if page.number is not None else 0
        return previous[number] if number in previous else ocr_page(page)

    pages: list[PageContent] = extract_pages(path, ocr=ocr)
    if have_pages != len(pages):
        session.execute(delete(Page).where(Page.document_id == doc.id))
        session.add_all(
            Page(
                document_id=doc.id,
                page_no=p.page_no,
                text=p.text,
                is_scanned=p.is_scanned,
                blocks=p.blocks,
            )
            for p in pages
        )
        session.commit()
        log.info("doc %d: stored %d pages", doc.id, len(pages))

    # 4. Chunks + embeddings in pgvector.
    n_chunks = session.scalar(select(func.count()).where(Chunk.document_id == doc.id)) or 0
    if n_chunks == 0:
        chunks = chunk_pages(pages, embeddings.token_offsets)
        vectors = embeddings.embed_passages([c.text for c in chunks])
        session.add_all(
            Chunk(
                document_id=doc.id,
                page_start=c.page_start,
                page_end=c.page_end,
                text=c.text,
                embedding=v,
            )
            for c, v in zip(chunks, vectors, strict=True)
        )
        session.commit()
        n_chunks = len(chunks)
        log.info("doc %d: stored %d chunks", doc.id, n_chunks)

    # 5-6. Section map (TOC + keyword + embedding search).
    result = locate_sections(pages, search=pgvector_search(session, doc.id))
    session.execute(
        update(Document).where(Document.id == doc.id).values(section_map=result.section_map)
    )
    session.commit()

    # 3. Tables from the financial-statement pages.
    table_pages = 0
    fin = result.section_map.get("restated_financials")
    if fin:
        tables = extract_tables(path, range(fin[0], fin[1] + 1))
        session.execute(update(Page).where(Page.document_id == doc.id).values(tables=None))
        for page_no, page_tables in tables.items():
            session.execute(
                update(Page)
                .where(Page.document_id == doc.id, Page.page_no == page_no)
                .values(tables=page_tables)
            )
        session.commit()
        table_pages = len(tables)

    label = f"{doc.doc_type} {path.name[:12]}"
    return DocOutcome(
        document_id=doc.id,
        label=label,
        n_pages=len(pages),
        scanned_pages=sum(p.is_scanned for p in pages),
        n_chunks=n_chunks,
        result=result,
        table_pages=table_pages,
    )


def _first_lines(session: Session, document_id: int, page_no: int, n: int = 3) -> str:
    text_ = session.scalar(
        select(Page.text).where(Page.document_id == document_id, Page.page_no == page_no)
    )
    lines = [ln.strip() for ln in (text_ or "").splitlines() if ln.strip()]
    return " / ".join(lines[:n])[:160]


def write_spot_check(session: Session, outcomes: list[DocOutcome], data_dir: Path) -> Path:
    names = dict(
        session.execute(
            select(Document.id, Company.name)
            .join(Ipo, Ipo.id == Document.ipo_id)
            .join(Company, Company.id == Ipo.company_id)
        ).all()
    )
    lines = [
        "# Phase 2 section-locator spot-check sheet",
        "",
        "Open each PDF at the start page and check the section heading is there, and that",
        "the page after the end page starts the next section. Pages are 1-based PDF pages.",
        "",
    ]
    for o in outcomes:
        r = o.result
        lines += [
            f"## {names.get(o.document_id, '?')} ({o.label}, document id {o.document_id})",
            "",
            f"{o.n_pages} pages ({o.scanned_pages} OCR'd), {o.n_chunks} chunks, tables on "
            f"{o.table_pages} pages, {len(r.toc_entries)} TOC entries, "
            f"printed-to-PDF offset {r.page_offset}. Missing: {', '.join(r.missing) or 'none'}",
            "",
            "| Section | Pages | Method | Heading on start page | Next section after end "
            "| Embedding: best chunk in range (similarity / rank in document) "
            "| First lines of start page | First lines of the page after the end "
            "| [ ] OK by hand |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for key in SECTION_KEYS:
            hit = r.hits.get(key)
            if hit is None:
                lines.append(f"| {key} | not found | | | | | | | |")
                continue
            score = f"{hit.embedding_score:.2f}" if hit.embedding_score is not None else "-"
            agrees = f"#{hit.embedding_rank}" if hit.embedding_rank is not None else "-"
            first = _first_lines(session, o.document_id, hit.start).replace("|", "/")
            after = _first_lines(session, o.document_id, hit.end + 1).replace("|", "/")
            lines.append(
                f"| {key} | {hit.start}-{hit.end} | {hit.method} | "
                f"{'yes' if hit.heading_found else 'NO'} | "
                f"{'yes' if hit.end_confirmed else 'CHECK'} | {score} / {agrees} | {first} "
                f"| {after} | |"
            )
        lines.append("")
    out = data_dir / "processed" / "section_check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.parsing.run", description=__doc__)
    parser.add_argument("--document-ids", help="comma list of documents.id")
    parser.add_argument("--doc-type", choices=["RHP", "DRHP"])
    parser.add_argument("--force", action="store_true", help="rebuild pages and chunks")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings or get_settings()

    from app.db.session import SessionLocal

    with SessionLocal() as session:
        query = select(Document).order_by(Document.id)
        if args.document_ids:
            ids = [int(x) for x in args.document_ids.split(",") if x.strip()]
            query = query.where(Document.id.in_(ids))
        if args.doc_type:
            query = query.where(Document.doc_type == args.doc_type)
        docs = [
            d for d in session.scalars(query).all() if (settings.data_dir / d.storage_path).exists()
        ]
        outcomes = []
        for doc in docs:
            outcome = process_document(session, doc, settings.data_dir, force=args.force)
            outcomes.append(outcome)
            log.info(
                "doc %d: %d/%d sections found, missing %s",
                doc.id,
                len(outcome.result.section_map),
                len(SECTION_KEYS),
                outcome.result.missing or "none",
            )
        report = write_spot_check(session, outcomes, settings.data_dir)
        log.info("spot-check sheet: %s (%d documents)", report, len(outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
