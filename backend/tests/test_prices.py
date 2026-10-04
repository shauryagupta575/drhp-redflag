from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from app.ingest.prices import (
    BENCHMARK,
    PriceRequest,
    build_requests,
    closes_from_frame,
    fetch_closes,
    nse_ticker,
)


def frame(series: dict[str, dict[str, float]]) -> pd.DataFrame:
    """A yf.download(group_by="ticker")-shaped frame: columns (ticker, field)."""
    cols = {}
    for ticker, closes in series.items():
        idx = pd.DatetimeIndex(list(closes))
        cols[(ticker, "Close")] = pd.Series(list(closes.values()), index=idx)
        cols[(ticker, "Open")] = pd.Series(list(closes.values()), index=idx)
    return pd.DataFrame(cols)


def test_nse_ticker() -> None:
    assert nse_ticker("TATATECH") == "TATATECH.NS"


def test_build_requests_windows_and_benchmark() -> None:
    today = date(2026, 1, 31)
    reqs = build_requests(
        [("OLD.NS", date(2024, 3, 1)), ("NEW.NS", date(2025, 12, 1)), ("FUT.NS", date(2026, 3, 1))],
        today,
    )
    by = {r.ticker: r for r in reqs}
    assert by["OLD.NS"].end == date(2024, 3, 1) + timedelta(days=365)
    assert by["NEW.NS"].end == today  # capped at today
    assert "FUT.NS" not in by  # not listed yet
    assert by[BENCHMARK] == PriceRequest(BENCHMARK, date(2024, 3, 1), today)


def test_build_requests_empty() -> None:
    assert build_requests([], date(2026, 1, 1)) == []


def test_closes_from_multiindex_frame_drops_missing() -> None:
    df = frame({"A.NS": {"2024-01-02": 10.5, "2024-01-03": float("nan")}})
    assert closes_from_frame(df, "A.NS") == {date(2024, 1, 2): Decimal("10.5")}
    assert closes_from_frame(df, "B.NS") == {}


def test_closes_from_flat_frame_and_empty() -> None:
    flat = pd.DataFrame({"Close": [1.23456789]}, index=pd.DatetimeIndex(["2024-05-06"]))
    assert closes_from_frame(flat, "ANY") == {date(2024, 5, 6): Decimal("1.2346")}
    assert closes_from_frame(pd.DataFrame(), "ANY") == {}


def test_fetch_closes_trims_to_each_window_and_reports_missing() -> None:
    calls: list[tuple[list[str], date, date]] = []

    def downloader(tickers: list[str], start: date, end: date) -> pd.DataFrame:
        calls.append((tickers, start, end))
        return frame(
            {
                "A.NS": {"2024-01-01": 1.0, "2024-01-10": 2.0, "2024-03-01": 3.0},
                BENCHMARK: {"2024-01-01": 100.0, "2024-03-01": 101.0},
            }
        )

    reqs = [
        PriceRequest("A.NS", date(2024, 1, 5), date(2024, 2, 1)),
        PriceRequest("GONE.NS", date(2024, 1, 5), date(2024, 2, 1)),
        PriceRequest(BENCHMARK, date(2024, 1, 1), date(2024, 3, 1)),
    ]
    closes, missing = fetch_closes(reqs, downloader=downloader, sleep=lambda s: None)
    assert closes["A.NS"] == {date(2024, 1, 10): Decimal("2.0")}
    assert set(closes[BENCHMARK]) == {date(2024, 1, 1), date(2024, 3, 1)}
    assert missing == ["GONE.NS"]
    # One batch spanning all windows; yfinance's end date is exclusive.
    assert calls == [([BENCHMARK, "A.NS", "GONE.NS"], date(2024, 1, 1), date(2024, 3, 2))]


def test_fetch_closes_batches_and_pauses_between_batches() -> None:
    pauses: list[float] = []
    batches: list[int] = []

    def downloader(tickers: list[str], start: date, end: date) -> pd.DataFrame:
        batches.append(len(tickers))
        return pd.DataFrame()

    reqs = [PriceRequest(f"T{i}.NS", date(2024, 1, 1), date(2024, 2, 1)) for i in range(85)]
    _, missing = fetch_closes(reqs, downloader=downloader, pause_s=2.0, sleep=pauses.append)
    assert batches == [40, 40, 5]
    assert pauses == [2.0, 2.0]
    assert len(missing) == 85
