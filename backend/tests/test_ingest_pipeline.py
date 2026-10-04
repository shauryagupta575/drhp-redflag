"""End-to-end ingest against a real Postgres, with SEBI and yfinance faked."""

import csv
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Company, Document, Ipo, Price
from app.ingest import run
from app.ingest.http import PoliteClient
from app.ingest.overrides import Overrides, parse_overrides
from app.ingest.prices import BENCHMARK
from app.ingest.sebi_scraper import load_index
from tests.helpers import make_pdf

NSE_CSV = (
    "﻿"
    '"COMPANY NAME","SECURITY TYPE","ISSUE PRICE","Symbol","ISSUE START DATE",'
    '"ISSUE END DATE","PRICE RANGE","DATE OF LISTING"\n'
    '"Alpha Limited","EQ","   114","ALPHA","22-DEC-2025","24-DEC-2025","Rs.108 to Rs.114","30-DEC-2025"\n'
    '"Beta Limited","BE","   100","BETA","30-SEP-2025","03-OCT-2025","Rs.95 to Rs.100","08-OCT-2025"\n'
    '"Gamma Limited","EQ","50","GAMMA","01-JUL-2024","03-JUL-2024","Rs.50","08-JUL-2024"\n'
    '"Small Co Limited","SME","90","SMALL","01-JUL-2024","03-JUL-2024","RS.90","08-JUL-2024"\n'
    '"Delta Limited - FPO","EQ","-","DELTAFPO","18-APR-2024","22-APR-2024","Rs.10","25-APR-2024"\n'
)
RHP_INDEX = (
    "2025-12-16|dec-2025/alpha-limited-rhp_1.html\n"
    "2025-09-25|sep-2025/beta-limited-rhp_2.html\n"
    "2025-09-26|sep-2025/beta-limited-addendum-to-rhp_3.html\n"
)
DRHP_INDEX = (
    "2025-03-01|mar-2025/alpha-limited_10.html\n"
    "2025-01-01|jan-2025/beta-limited-drhp_11.html\n"
    "2023-10-01|oct-2023/gamma-limited-drhp_12.html\n"
)
PAGES = {  # filing page -> (pdf path or None, page count)
    "/filings/public-issues/dec-2025/alpha-limited-rhp_1.html": ("/sebi_data/a_rhp.pdf", 4),
    "/filings/public-issues/mar-2025/alpha-limited_10.html": ("/sebi_data/a_drhp.pdf", 3),
    "/filings/public-issues/sep-2025/beta-limited-rhp_2.html": ("/sebi_data/b_rhp.pdf", 2),
    "/filings/public-issues/jan-2025/beta-limited-drhp_11.html": (None, 0),  # broken page
    "/filings/public-issues/oct-2023/gamma-limited-drhp_12.html": ("/sebi_data/g_drhp.pdf", 5),
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "raw" / "nse").mkdir(parents=True)
    (tmp_path / "raw" / "sebi").mkdir(parents=True)
    (tmp_path / "raw" / "nse" / "IPO-PastIssue-01-01-2019-to-31-12-2025.csv").write_text(
        NSE_CSV, encoding="utf-8"
    )
    (tmp_path / "raw" / "sebi" / "rhp_index.txt").write_text(RHP_INDEX)
    (tmp_path / "raw" / "sebi" / "drhp_index.txt").write_text(DRHP_INDEX)
    return tmp_path


class FakeSebi:
    def __init__(self, block_after: int | None = None) -> None:
        self.requests: list[str] = []
        self.block_after = block_after
        self.pdfs = {pdf: make_pdf(n) for pdf, n in PAGES.values() if pdf}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /js\n")
        if self.block_after is not None and len(self.requests) > self.block_after:
            return httpx.Response(530, text="Unauthorized Request Blocked")
        if path in PAGES:
            pdf, _ = PAGES[path]
            iframe = f"<iframe src='../../../web/?file={pdf}'></iframe>" if pdf else ""
            return httpx.Response(200, text=f"<html><body>{iframe}</body></html>")
        if path in self.pdfs:
            return httpx.Response(200, content=self.pdfs[path])
        return httpx.Response(404)


def client(tmp: Path, sebi: FakeSebi) -> PoliteClient:
    return PoliteClient(
        tmp / "cache",
        "test-agent",
        min_interval_s=0,
        transport=httpx.MockTransport(sebi),
        sleep=lambda s: None,
    )


def run_documents(
    session: Session, data_dir: Path, sebi: FakeSebi, overrides: Overrides | None = None
) -> run.DocResult:
    overrides = overrides or Overrides()
    uni = run.load_universe(data_dir, overrides)
    run.step_universe(session, uni)
    matches = run.resolve_matches(
        uni,
        load_index(data_dir / "raw" / "sebi" / "rhp_index.txt"),
        load_index(data_dir / "raw" / "sebi" / "drhp_index.txt"),
        overrides,
    )
    with client(data_dir, sebi) as c:
        return run.step_documents(session, c, uni, matches, data_dir)


def fake_downloader(calls: list[list[str]]) -> Callable[[list[str], date, date], pd.DataFrame]:
    def download(tickers: list[str], start: date, end: date) -> pd.DataFrame:
        calls.append(tickers)
        idx = pd.bdate_range(start, end, inclusive="left")
        cols = {(t, "Close"): pd.Series(100.0, index=idx) for t in tickers if t != "GAMMA.NS"}
        return pd.DataFrame(cols)

    return download


def test_universe_step_is_idempotent(db_session: Session, data_dir: Path) -> None:
    uni = run.load_universe(data_dir, Overrides())
    assert [i.symbol for i in uni.issues] == ["ALPHA", "BETA", "GAMMA"]  # newest first
    run.step_universe(db_session, uni)
    run.step_universe(db_session, run.load_universe(data_dir, Overrides()))
    assert db_session.scalar(select(func.count()).select_from(Company)) == 3
    assert db_session.scalar(select(func.count()).select_from(Ipo)) == 3
    alpha = db_session.scalars(select(Ipo).join(Company).where(Company.nse_symbol == "ALPHA")).one()
    assert alpha.issue_price == Decimal("114.00")
    assert alpha.listing_date == date(2025, 12, 30)
    assert alpha.exchange == "NSE"


def test_overrides_add_listing_date_and_exclude(db_session: Session, data_dir: Path) -> None:
    ov = parse_overrides(
        "nse_symbol,field,value,source_note\nGAMMA,exclude,yes,test\nALPHA,issue_price,115,test\n"
    )
    uni = run.load_universe(data_dir, ov)
    assert [i.symbol for i in uni.issues] == ["ALPHA", "BETA"]
    assert ("GAMMA", "excluded in overrides.csv") in {(i.symbol, r) for i, r in uni.excluded}
    assert uni.issues[0].issue_price == Decimal("115")


def test_documents_downloaded_stored_and_skipped_on_rerun(
    db_session: Session, data_dir: Path
) -> None:
    sebi = FakeSebi()
    result = run_documents(db_session, data_dir, sebi)
    assert result.downloaded == 4
    assert result.unmatched == ["GAMMA RHP"]
    assert len(result.failed) == 1 and result.failed[0].startswith("BETA DRHP: no PDF link")
    assert result.blocked is None

    docs = db_session.scalars(select(Document).order_by(Document.id)).all()
    assert {(d.doc_type, d.n_pages) for d in docs} == {
        ("RHP", 4),
        ("DRHP", 3),
        ("RHP", 2),
        ("DRHP", 5),
    }
    for d in docs:
        assert d.storage_path == f"pdfs/{d.sha256}.pdf"  # relative to DATA_DIR
        assert (data_dir / d.storage_path).exists()
        assert d.source_url.startswith("https://www.sebi.gov.in/sebi_data/")
    alpha_rhp = next(d for d in docs if d.n_pages == 4)
    assert alpha_rhp.filed_on == date(2025, 12, 16)

    second = FakeSebi()
    again = run_documents(db_session, data_dir, second)
    assert again.downloaded == 0 and again.skipped_existing == 4
    # Only the broken filing is retried, and its page now comes from the disk cache:
    # the re-run makes no network requests at all.
    assert second.requests == []
    assert db_session.scalar(select(func.count()).select_from(Document)) == 4


def test_documents_stop_when_blocked(db_session: Session, data_dir: Path) -> None:
    sebi = FakeSebi(block_after=3)  # robots, one filing page, one PDF, then blocked
    result = run_documents(db_session, data_dir, sebi)
    assert result.blocked is not None
    assert result.downloaded == 1
    assert len(sebi.requests) == 4  # stopped at the first block, no retries
    assert db_session.scalar(select(func.count()).select_from(Document)) == 1


def test_prices_stored_and_not_refetched(db_session: Session, data_dir: Path) -> None:
    uni = run.load_universe(data_dir, Overrides())
    run.step_universe(db_session, uni)
    calls: list[list[str]] = []
    today = date(2026, 2, 15)
    missing = run.step_prices(
        db_session, uni, Overrides(), today, downloader=fake_downloader(calls)
    )
    assert missing == ["GAMMA.NS"]
    rows = dict(db_session.execute(select(Price.ticker, func.count()).group_by(Price.ticker)).all())
    assert set(rows) == {"ALPHA.NS", "BETA.NS", BENCHMARK}
    first_alpha = db_session.scalar(select(func.min(Price.date)).where(Price.ticker == "ALPHA.NS"))
    assert first_alpha == date(2025, 12, 30)  # starts at listing

    calls.clear()
    run.step_prices(db_session, uni, Overrides(), today, downloader=fake_downloader(calls))
    assert calls == [["GAMMA.NS"]]  # only the ticker still missing is retried


def test_report_and_universe_csv(db_session: Session, data_dir: Path) -> None:
    result = run_documents(db_session, data_dir, FakeSebi())
    uni = run.load_universe(data_dir, Overrides())
    missing = run.step_prices(
        db_session, uni, Overrides(), date(2026, 2, 15), downloader=fake_downloader([])
    )
    report = run.step_report(db_session, uni, Overrides(), data_dir, result, missing)
    text = report.read_text()
    assert "IPOs in universe: 3" in text
    assert "With RHP: 2; with DRHP: 2; with at least one document: 3" in text
    assert "DELTAFPO (Delta Limited - FPO): follow-on offer (FPO)" in text
    assert "- GAMMA RHP" in text and "- GAMMA.NS" in text
    with (data_dir / "processed" / "universe.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert [r["nse_symbol"] for r in rows] == ["ALPHA", "BETA", "GAMMA"]
    alpha = rows[0]
    assert alpha["rhp_pages"] == "4" and alpha["drhp_pages"] == "3"
    assert alpha["ticker"] == "ALPHA.NS" and int(alpha["price_rows"]) > 0
    assert rows[2]["price_rows"] == "0"


def test_cli_rejects_unknown_step() -> None:
    with pytest.raises(SystemExit):
        run.main(["--steps", "universe,bogus"])


def test_missing_nse_csv_is_explained(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="README"):
        run.load_universe(tmp_path, Overrides())
