from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest.exchange_data import NseIssue
from app.ingest.overrides import DEFAULT_PATH, load_overrides, parse_overrides

HEADER = "nse_symbol,field,value,source_note\n"


def issue(symbol: str) -> NseIssue:
    return NseIssue("Co Limited", "EQ", symbol, None, date(2022, 10, 31), None, None, "")


def test_committed_seed_file_parses() -> None:
    assert DEFAULT_PATH.exists()
    load_overrides(DEFAULT_PATH)


def test_missing_file_means_no_overrides(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "nope.csv").by_symbol == {}


def test_apply_fills_dates_and_price() -> None:
    ov = parse_overrides(
        HEADER
        + "DCX,listing_date,2022-11-11,checked on exchange site\n"
        + "DCX,issue_price,207,checked on exchange site\n"
        + "DCX,yf_ticker,DCXINDIA.NS,renamed\n"
    )
    out = ov.apply(issue("DCX"))
    assert out.listing_date == date(2022, 11, 11)
    assert out.issue_price == Decimal("207")
    assert out.open_date == date(2022, 10, 31)  # untouched
    assert ov.get("DCX", "yf_ticker") == "DCXINDIA.NS"
    assert ov.apply(issue("OTHER")) == issue("OTHER")


def test_exclude_flag() -> None:
    ov = parse_overrides(HEADER + "BAD,exclude,yes,not an IPO\n\n")
    assert ov.excluded("BAD") and not ov.excluded("GOOD")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("symbol,field\n", "header"),
        (HEADER + "X,colour,red,why\n", "unknown field"),
        (HEADER + "X,exclude\n", "expected 4 columns"),
    ],
)
def test_invalid_files_are_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_overrides(text)
