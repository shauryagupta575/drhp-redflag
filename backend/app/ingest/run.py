"""Phase 1 ingest: IPO universe, offer documents and prices, reproducible in one command.

    python -m app.ingest.run                      # all steps
    python -m app.ingest.run --steps documents    # one step
    python -m app.ingest.run --limit 5            # first 5 IPOs (newest listings first)

Inputs (gitignored, under DATA_DIR, see README):
    raw/nse/IPO-PastIssue-*.csv     NSE "Past Issues" CSV (downloaded by hand)
    raw/sebi/rhp_index.txt          SEBI RHP listing index
    raw/sebi/drhp_index.txt         SEBI DRHP listing index
Outputs:
    Postgres tables companies, ipos, documents, prices
    pdfs/<sha256>.pdf, cache/http/*, processed/universe.csv, processed/ingest_report.md
"""

import argparse
import csv
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Company, Document, Ipo
from app.ingest import prices as prices_mod
from app.ingest import store
from app.ingest.exchange_data import NseIssue, parse_past_issues, select_universe
from app.ingest.http import BlockedError, PoliteClient
from app.ingest.overrides import Overrides, load_overrides
from app.ingest.sebi_scraper import (
    FilingMatch,
    SebiFiling,
    extract_pdf_urls,
    load_index,
    match_filings,
    store_pdf,
)

log = logging.getLogger("app.ingest")

STEPS = ("universe", "documents", "prices", "report")


@dataclass
class Universe:
    issues: list[NseIssue]
    excluded: list[tuple[NseIssue, str]]
    ipo_ids: dict[str, int] = field(default_factory=dict)  # symbol -> ipos.id


@dataclass
class DocResult:
    downloaded: int = 0
    skipped_existing: int = 0
    unmatched: list[str] = field(default_factory=list)  # "SYMBOL DOC_TYPE"
    failed: list[str] = field(default_factory=list)
    blocked: str | None = None


def nse_csv_path(data_dir: Path) -> Path:
    files = sorted((data_dir / "raw" / "nse").glob("IPO-PastIssue-*.csv"))
    if not files:
        raise FileNotFoundError(
            f"No NSE past-issues CSV in {data_dir / 'raw' / 'nse'} (see README, 'Data inputs')"
        )
    return files[-1]


def load_universe(data_dir: Path, overrides: Overrides) -> Universe:
    issues = parse_past_issues(nse_csv_path(data_dir).read_text(encoding="utf-8"))
    issues = [overrides.apply(i) for i in issues]
    universe, excluded = select_universe(issues)
    kept = [i for i in universe if not overrides.excluded(i.symbol)]
    excluded += [(i, "excluded in overrides.csv") for i in universe if overrides.excluded(i.symbol)]
    # Newest listings first, so --limit picks recent IPOs.
    kept.sort(key=lambda i: (i.listing_date or date.min, i.symbol), reverse=True)
    return Universe(kept, excluded)


def step_universe(session: Session, uni: Universe) -> None:
    for issue in uni.issues:
        uni.ipo_ids[issue.symbol] = store.upsert_ipo(session, issue)
    session.commit()
    log.info("universe: %d IPOs stored, %d rows excluded", len(uni.issues), len(uni.excluded))


def _ipo_ids(session: Session, uni: Universe) -> dict[str, int]:
    if uni.ipo_ids:
        return uni.ipo_ids
    rows = session.execute(
        select(Company.nse_symbol, Ipo.id).join(Ipo, Ipo.company_id == Company.id)
    ).all()
    wanted = {i.symbol for i in uni.issues}
    return {sym: ipo_id for sym, ipo_id in rows if sym in wanted}


def _filing_for_override(path: str, index: list[SebiFiling]) -> SebiFiling:
    for filing in index:
        if filing.path == path:
            return filing
    raise ValueError(f"override path {path!r} is not in the SEBI index")


def resolve_matches(
    uni: Universe, rhps: list[SebiFiling], drhps: list[SebiFiling], overrides: Overrides
) -> dict[str, FilingMatch]:
    matches = {}
    for issue in uni.issues:
        m = match_filings(issue, rhps, drhps)
        rhp_path = overrides.get(issue.symbol, "rhp_path")
        drhp_path = overrides.get(issue.symbol, "drhp_path")
        if rhp_path:
            m = FilingMatch(_filing_for_override(rhp_path, rhps), m.drhp, None, m.drhp_score)
        if drhp_path:
            m = FilingMatch(m.rhp, _filing_for_override(drhp_path, drhps), m.rhp_score, None)
        matches[issue.symbol] = m
    return matches


def step_documents(
    session: Session,
    client: PoliteClient,
    uni: Universe,
    matches: dict[str, FilingMatch],
    data_dir: Path,
) -> DocResult:
    result = DocResult()
    ids = _ipo_ids(session, uni)
    pdf_dir = data_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    # All RHPs first (the preferred document), then DRHPs.
    for doc_type in ("RHP", "DRHP"):
        for n, issue in enumerate(uni.issues, start=1):
            ipo_id = ids.get(issue.symbol)
            if ipo_id is None:
                raise RuntimeError(f"{issue.symbol} is not in the database; run the universe step")
            match = matches[issue.symbol]
            filing = match.rhp if doc_type == "RHP" else match.drhp
            label = f"{issue.symbol} {doc_type}"
            if filing is None:
                result.unmatched.append(label)
                continue
            existing = store.get_document(session, ipo_id, doc_type)
            if existing is not None and (data_dir / existing.storage_path).exists():
                result.skipped_existing += 1
                continue
            try:
                pdf_urls = extract_pdf_urls(client.get_text(filing.url), filing.url)
                if not pdf_urls:
                    raise ValueError(f"no PDF link on {filing.url}")
                stored = store_pdf(client, pdf_urls[0], pdf_dir)
            except BlockedError as exc:
                result.blocked = str(exc)
                log.error("documents: blocked, stopping (download manually): %s", exc)
                session.commit()
                return result
            except Exception as exc:  # one bad filing must not stop the run
                result.failed.append(f"{label}: {exc}")
                log.warning("documents: %s failed: %s", label, exc)
                continue
            store.upsert_document(
                session,
                ipo_id=ipo_id,
                doc_type=doc_type,
                source_url=stored.source_url,
                sha256=stored.sha256,
                n_pages=stored.n_pages,
                filed_on=filing.filed_on,
                storage_path=str(stored.storage_path.relative_to(data_dir)),
            )
            session.commit()
            result.downloaded += 1
            log.info(
                "documents: %s [%d/%d] %s %d pages",
                doc_type, n, len(uni.issues), issue.symbol, stored.n_pages,
            )  # fmt: skip
    return result


def ticker_for(issue: NseIssue, overrides: Overrides) -> str:
    return overrides.get(issue.symbol, "yf_ticker") or prices_mod.nse_ticker(issue.symbol)


def step_prices(
    session: Session,
    uni: Universe,
    overrides: Overrides,
    today: date,
    downloader: prices_mod.Downloader = prices_mod.yfinance_downloader,
) -> list[str]:
    listings = [(ticker_for(i, overrides), i.listing_date) for i in uni.issues if i.listing_date]
    requests = prices_mod.build_requests(listings, today)
    have = store.price_coverage(session, [r.ticker for r in requests])
    # Skip tickers whose stored series already spans the requested window (within a week).
    todo = [
        r
        for r in requests
        if not (
            r.ticker in have
            and (have[r.ticker][0] - r.start).days <= 7
            and (r.end - have[r.ticker][1]).days <= 7
        )
    ]
    log.info("prices: %d tickers to fetch, %d already stored", len(todo), len(requests) - len(todo))
    closes, missing = prices_mod.fetch_closes(todo, downloader=downloader)
    for ticker, series in closes.items():
        store.upsert_prices(session, ticker, series)
    session.commit()
    if missing:
        log.warning("prices: no data for %d tickers: %s", len(missing), ", ".join(missing))
    return missing


def step_report(
    session: Session,
    uni: Universe,
    overrides: Overrides,
    data_dir: Path,
    doc_result: DocResult | None,
    missing_prices: list[str] | None,
) -> Path:
    ids = _ipo_ids(session, uni)
    tickers = {i.symbol: ticker_for(i, overrides) for i in uni.issues}
    coverage = store.price_coverage(session, list(tickers.values()) + [prices_mod.BENCHMARK])
    docs: dict[tuple[int, str], Document] = {
        (d.ipo_id, d.doc_type): d for d in session.scalars(select(Document)).all()
    }
    out_dir = data_dir / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)

    counts = {"rhp": 0, "drhp": 0, "any_doc": 0, "prices": 0}
    with (out_dir / "universe.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "nse_symbol", "company", "open_date", "close_date", "listing_date",
                "issue_price", "rhp_pages", "drhp_pages", "ticker", "price_rows",
                "first_price", "last_price",
            ]
        )  # fmt: skip
        for issue in uni.issues:
            ipo_id = ids.get(issue.symbol)
            rhp = docs.get((ipo_id, "RHP")) if ipo_id else None
            drhp = docs.get((ipo_id, "DRHP")) if ipo_id else None
            cov = coverage.get(tickers[issue.symbol])
            counts["rhp"] += rhp is not None
            counts["drhp"] += drhp is not None
            counts["any_doc"] += rhp is not None or drhp is not None
            counts["prices"] += cov is not None
            writer.writerow(
                [
                    issue.symbol, issue.company, issue.open_date, issue.close_date,
                    issue.listing_date, issue.issue_price,
                    rhp.n_pages if rhp else "", drhp.n_pages if drhp else "",
                    tickers[issue.symbol], cov[2] if cov else 0,
                    cov[0] if cov else "", cov[1] if cov else "",
                ]
            )  # fmt: skip

    pdfs = {d.sha256 for d in docs.values()}
    lines = [
        "# Phase 1 ingest report",
        "",
        f"- IPOs in universe: {len(uni.issues)}",
        f"- With RHP: {counts['rhp']}; with DRHP: {counts['drhp']}; "
        f"with at least one document: {counts['any_doc']}",
        f"- Distinct PDFs stored: {len(pdfs)}",
        f"- IPOs with price series: {counts['prices']}",
        f"- Benchmark {prices_mod.BENCHMARK} rows: "
        f"{coverage[prices_mod.BENCHMARK][2] if prices_mod.BENCHMARK in coverage else 0}",
        "",
        "## Excluded NSE rows",
        "",
        *[f"- {i.symbol} ({i.company}): {reason}" for i, reason in uni.excluded],
    ]
    if doc_result is not None:
        lines += ["", "## Documents not found in SEBI indices", ""]
        lines += [f"- {u}" for u in doc_result.unmatched] or ["- none"]
        lines += ["", "## Document download failures", ""]
        lines += [f"- {f}" for f in doc_result.failed] or ["- none"]
        if doc_result.blocked:
            lines += ["", f"**Stopped: blocked by site** ({doc_result.blocked})"]
    if missing_prices is not None:
        lines += ["", "## Tickers with no yfinance data", ""]
        lines += [f"- {t}" for t in missing_prices] or ["- none"]
    report = out_dir / "ingest_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingest.run", description=__doc__)
    parser.add_argument("--steps", default=",".join(STEPS), help=f"comma list of {STEPS}")
    parser.add_argument("--limit", type=int, help="only the N most recently listed IPOs")
    parser.add_argument("--symbols", help="comma list of NSE symbols to restrict to")
    args = parser.parse_args(argv)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(steps) - set(STEPS)
    if unknown:
        parser.error(f"unknown steps: {sorted(unknown)}")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = settings or get_settings()
    data_dir = settings.data_dir
    overrides = load_overrides()
    uni = load_universe(data_dir, overrides)
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",")}
        uni.issues = [i for i in uni.issues if i.symbol in wanted]
    if args.limit:
        uni.issues = uni.issues[: args.limit]

    from app.db.session import SessionLocal

    doc_result: DocResult | None = None
    missing_prices: list[str] | None = None
    with SessionLocal() as session:
        if "universe" in steps:
            step_universe(session, uni)
        if "documents" in steps:
            rhps = load_index(data_dir / "raw" / "sebi" / "rhp_index.txt")
            drhps = load_index(data_dir / "raw" / "sebi" / "drhp_index.txt")
            matches = resolve_matches(uni, rhps, drhps, overrides)
            with PoliteClient(
                data_dir / "cache", settings.http_user_agent, settings.http_min_interval_s
            ) as client:
                doc_result = step_documents(session, client, uni, matches, data_dir)
            log.info(
                "documents: %d downloaded, %d already stored, %d not in SEBI indices, %d failed",
                doc_result.downloaded,
                doc_result.skipped_existing,
                len(doc_result.unmatched),
                len(doc_result.failed),
            )
        if "prices" in steps:
            missing_prices = step_prices(session, uni, overrides, date.today())
        if "report" in steps:
            report = step_report(session, uni, overrides, data_dir, doc_result, missing_prices)
            log.info("report written to %s", report)
    return 1 if doc_result is not None and doc_result.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
