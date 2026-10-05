"""ORM models. Tables follow build guide Section 6.2; later phases add the rest."""

from datetime import date
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    nse_symbol: Mapped[str | None] = mapped_column(String(32), unique=True)
    bse_code: Mapped[str | None] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(String(120))

    ipos: Mapped[list["Ipo"]] = relationship(back_populates="company")


class Ipo(Base):
    __tablename__ = "ipos"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    open_date: Mapped[date | None] = mapped_column(Date)
    # Phase 1 step 2 lists issue open/close dates; Section 6.2 shows open_date only.
    close_date: Mapped[date | None] = mapped_column(Date)
    listing_date: Mapped[date | None] = mapped_column(Date)
    issue_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    issue_size_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fresh_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    ofs_cr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    exchange: Mapped[str | None] = mapped_column(String(16))

    company: Mapped[Company] = relationship(back_populates="ipos")
    documents: Mapped[list["Document"]] = relationship(back_populates="ipo")

    __table_args__ = (UniqueConstraint("company_id", "open_date", name="uq_ipos_company_open"),)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(8))  # "DRHP" or "RHP"
    source_url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    n_pages: Mapped[int | None] = mapped_column(Integer)
    filed_on: Mapped[date | None] = mapped_column(Date)
    storage_path: Mapped[str] = mapped_column(Text)
    # Phase 2 step 6: {"related_party": [312, 318], ...} in PDF page numbers (1-based).
    section_map: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    ipo: Mapped[Ipo] = relationship(back_populates="documents")

    __table_args__ = (UniqueConstraint("ipo_id", "doc_type", name="uq_documents_ipo_type"),)


class Price(Base):
    __tablename__ = "prices"

    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(14, 4))


class Page(Base):
    """Page-level text for citations (Section 6.2), one row per PDF page."""

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(Integer)  # 1-based PDF page number
    text: Mapped[str] = mapped_column(Text)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 2 step 1: text block coordinates for later highlighting,
    # [[x0, y0, x1, y1, text], ...] in PDF points.
    blocks: Mapped[list[Any] | None] = mapped_column(JSONB)
    # Phase 2 step 3: pdfplumber tables on financial-statement pages, [[[cell, ...], ...], ...].
    tables: Mapped[list[Any] | None] = mapped_column(JSONB)

    __table_args__ = (UniqueConstraint("document_id", "page_no", name="uq_pages_document_page"),)


class Chunk(Base):
    """~500-token windows of page text with embeddings, for semantic section search."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM))

    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
