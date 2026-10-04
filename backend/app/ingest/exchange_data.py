"""Parse NSE's "Past Issues" CSV into the IPO universe.

Input is the file NSE offers via the Download (.csv) button on
nseindia.com/market-data/all-upcoming-issues-ipo (Past Issues tab). NSE's terms of use
prohibit automated collection, so this file is downloaded by hand and placed in
data/raw/nse/ (see README); this module only reads it.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# NSE security types for mainboard equity. "EQ" is the normal series; "BE" is the
# trade-for-trade mainboard series. SME, debt, InvIT ("IV"), REIT ("RR") etc. are excluded.
MAINBOARD_TYPES = frozenset({"EQ", "BE"})

_EXPECTED_HEADER = [
    "COMPANY NAME",
    "SECURITY TYPE",
    "ISSUE PRICE",
    "Symbol",
    "ISSUE START DATE",
    "ISSUE END DATE",
    "PRICE RANGE",
    "DATE OF LISTING",
]


@dataclass(frozen=True)
class NseIssue:
    company: str
    security_type: str
    symbol: str
    issue_price: Decimal | None
    open_date: date | None
    close_date: date | None
    listing_date: date | None
    price_range: str


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value or value == "-":
        return None
    return datetime.strptime(value, "%d-%b-%Y").date()


def _parse_price(value: str) -> Decimal | None:
    value = value.strip()
    if not value or value in {"-", "######"}:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def parse_past_issues(text: str) -> list[NseIssue]:
    """Parse the CSV text (BOM tolerated) into one record per row, all security types."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = next(reader, None)
    if header != _EXPECTED_HEADER:
        raise ValueError(f"Unexpected NSE past-issues header: {header!r}")
    issues = []
    for row in reader:
        if not row:
            continue
        if len(row) != len(_EXPECTED_HEADER):
            raise ValueError(f"Malformed NSE past-issues row: {row!r}")
        name, sec_type, price, symbol, start, end, price_range, listing = row
        issues.append(
            NseIssue(
                company=" ".join(name.split()),
                security_type=sec_type.strip(),
                symbol=symbol.strip(),
                issue_price=_parse_price(price),
                open_date=_parse_date(start),
                close_date=_parse_date(end),
                listing_date=_parse_date(listing),
                price_range=price_range.strip(),
            )
        )
    return issues


_PARTLY_PAID_SYMBOL = re.compile(r"PP\d*$")


def exclusion_reason(issue: NseIssue) -> str | None:
    """Why a mainboard EQ/BE row is not part of the IPO universe, or None if it is.

    Rules use only what NSE's file itself shows: follow-on offers are named "FPO" or list
    before they open (the company was already listed); "PP"/"PP<n>" symbols are partly-paid
    shares; withdrawn issues are named so and never list.
    """
    name = issue.company.upper()
    if "FPO" in name:
        return "follow-on offer (FPO)"
    if "ISSUE WITHDRAWN" in name:
        return "issue withdrawn"
    if _PARTLY_PAID_SYMBOL.search(issue.symbol):
        return "partly-paid shares"
    if issue.listing_date is None:
        return "no listing date in NSE file"
    if issue.open_date is not None and issue.listing_date < issue.open_date:
        return "listed before issue opened (follow-on offer)"
    return None


def select_universe(
    issues: list[NseIssue],
) -> tuple[list[NseIssue], list[tuple[NseIssue, str]]]:
    """Split rows into the mainboard IPO universe and excluded rows with reasons.

    Only EQ/BE security types are considered. NSE's file repeats some rows; the first
    occurrence of each symbol is kept (the file is newest first).
    """
    seen: set[str] = set()
    universe: list[NseIssue] = []
    excluded: list[tuple[NseIssue, str]] = []
    for issue in issues:
        if issue.security_type not in MAINBOARD_TYPES or not issue.symbol:
            continue
        reason = exclusion_reason(issue)
        if reason is not None:
            excluded.append((issue, reason))
            continue
        if issue.symbol in seen:
            excluded.append((issue, "duplicate symbol"))
            continue
        seen.add(issue.symbol)
        universe.append(issue)
    return universe, excluded
