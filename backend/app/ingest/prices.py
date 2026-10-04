"""Daily closes from yfinance: each IPO from listing date to +12 months, plus Nifty 50.

`close` is yfinance's "Close" with auto_adjust=False: adjusted for splits but not for
dividends. Tickers are NSE symbols with the ".NS" suffix; the benchmark is "^NSEI".
"""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

log = logging.getLogger(__name__)

BENCHMARK = "^NSEI"
WINDOW = timedelta(days=365)
BATCH_SIZE = 40

# (tickers, start, end_exclusive) -> DataFrame shaped like yf.download(group_by="ticker")
Downloader = Callable[[list[str], date, date], pd.DataFrame]


@dataclass(frozen=True)
class PriceRequest:
    ticker: str
    start: date  # listing date
    end: date  # inclusive: listing date + 12 months, capped at today


def nse_ticker(symbol: str) -> str:
    return f"{symbol}.NS"


def build_requests(listings: Iterable[tuple[str, date]], today: date) -> list[PriceRequest]:
    """One request per (ticker, listing date), plus the benchmark over the union of windows."""
    requests = [
        PriceRequest(ticker, listed, min(listed + WINDOW, today))
        for ticker, listed in listings
        if listed <= today
    ]
    if requests:
        requests.append(
            PriceRequest(
                BENCHMARK,
                min(r.start for r in requests),
                max(r.end for r in requests),
            )
        )
    return requests


def yfinance_downloader(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    frame = yf.download(
        tickers=tickers,
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=False,
    )
    return frame if frame is not None else pd.DataFrame()


def closes_from_frame(frame: pd.DataFrame, ticker: str) -> dict[date, Decimal]:
    """Extract one ticker's non-missing closes from a group_by="ticker" frame."""
    if frame.empty:
        return {}
    if isinstance(frame.columns, pd.MultiIndex):
        if ticker not in frame.columns.get_level_values(0):
            return {}
        series = frame[ticker]["Close"]
    else:
        if "Close" not in frame.columns:
            return {}
        series = frame["Close"]
    out: dict[date, Decimal] = {}
    for idx, value in series.dropna().items():
        day = pd.Timestamp(idx).date()
        out[day] = Decimal(str(round(float(value), 4)))
    return out


def fetch_closes(
    requests: list[PriceRequest],
    downloader: Downloader = yfinance_downloader,
    pause_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, dict[date, Decimal]], list[str]]:
    """Download closes in batches; returns ({ticker: {date: close}}, tickers with no data).

    Each batch spans the union of its tickers' windows; rows are then trimmed per ticker.
    """
    result: dict[str, dict[date, Decimal]] = {}
    missing: list[str] = []
    ordered = sorted(requests, key=lambda r: r.start)
    for i in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[i : i + BATCH_SIZE]
        start = min(r.start for r in batch)
        end = max(r.end for r in batch) + timedelta(days=1)  # yfinance end is exclusive
        if i:
            sleep(pause_s)
        frame = downloader([r.ticker for r in batch], start, end)
        for req in batch:
            closes = {
                d: c
                for d, c in closes_from_frame(frame, req.ticker).items()
                if req.start <= d <= req.end
            }
            if closes:
                result[req.ticker] = closes
            else:
                missing.append(req.ticker)
        log.info("prices: batch %d-%d done", i + 1, i + len(batch))
    return result, missing
