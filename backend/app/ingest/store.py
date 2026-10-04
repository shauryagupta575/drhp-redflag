"""Idempotent database writes for the ingest pipeline (Postgres upserts)."""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Company, Document, Ipo, Price
from app.ingest.exchange_data import NseIssue


def upsert_ipo(session: Session, issue: NseIssue, exchange: str = "NSE") -> int:
    """Insert or update the company (keyed by NSE symbol) and its IPO; returns the IPO id."""
    company_id = session.execute(
        insert(Company)
        .values(name=issue.company, nse_symbol=issue.symbol)
        .on_conflict_do_update(index_elements=["nse_symbol"], set_={"name": issue.company})
        .returning(Company.id)
    ).scalar_one()
    values = {
        "company_id": company_id,
        "open_date": issue.open_date,
        "close_date": issue.close_date,
        "listing_date": issue.listing_date,
        "issue_price": issue.issue_price,
        "exchange": exchange,
    }
    ipo_id: int = session.execute(
        insert(Ipo)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_ipos_company_open",
            set_={k: v for k, v in values.items() if k not in {"company_id", "open_date"}},
        )
        .returning(Ipo.id)
    ).scalar_one()
    return ipo_id


def get_document(session: Session, ipo_id: int, doc_type: str) -> Document | None:
    return session.scalars(
        select(Document).where(Document.ipo_id == ipo_id, Document.doc_type == doc_type)
    ).one_or_none()


def upsert_document(
    session: Session,
    *,
    ipo_id: int,
    doc_type: str,
    source_url: str,
    sha256: str,
    n_pages: int,
    filed_on: date | None,
    storage_path: str,
) -> None:
    values = {
        "ipo_id": ipo_id,
        "doc_type": doc_type,
        "source_url": source_url,
        "sha256": sha256,
        "n_pages": n_pages,
        "filed_on": filed_on,
        "storage_path": storage_path,
    }
    session.execute(
        insert(Document)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_documents_ipo_type",
            set_={k: v for k, v in values.items() if k not in {"ipo_id", "doc_type"}},
        )
    )


def upsert_prices(session: Session, ticker: str, closes: dict[date, Decimal]) -> int:
    if not closes:
        return 0
    rows = [{"ticker": ticker, "date": d, "close": c} for d, c in sorted(closes.items())]
    stmt = insert(Price).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["ticker", "date"], set_={"close": stmt.excluded.close}
        )
    )
    return len(rows)


def price_coverage(session: Session, tickers: Iterable[str]) -> dict[str, tuple[date, date, int]]:
    """{ticker: (first date, last date, row count)} for tickers already stored."""
    rows = session.execute(
        select(Price.ticker, func.min(Price.date), func.max(Price.date), func.count())
        .where(Price.ticker.in_(list(tickers)))
        .group_by(Price.ticker)
    ).all()
    return {t: (lo, hi, n) for t, lo, hi, n in rows}
