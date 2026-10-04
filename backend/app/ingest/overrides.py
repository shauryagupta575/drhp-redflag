"""Hand-curated corrections (build guide Phase 1 step 2: "Fill gaps by hand in a CSV").

backend/app/ingest/seed/overrides.csv has columns nse_symbol,field,value,source_note.
Supported fields:
  listing_date, open_date, close_date   YYYY-MM-DD (e.g. an IPO NSE's file lists without
                                        a listing date)
  issue_price                           number
  exclude                               any value; drops the IPO from the universe
  yf_ticker                             Yahoo ticker to use instead of <SYMBOL>.NS
  rhp_path, drhp_path                   SEBI filing path "<month-folder>/<slug>_<id>.html"
Every row should say where the value came from in source_note.
"""

import csv
import io
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.ingest.exchange_data import NseIssue

DEFAULT_PATH = Path(__file__).parent / "seed" / "overrides.csv"
_HEADER = ["nse_symbol", "field", "value", "source_note"]
_FIELDS = {
    "listing_date",
    "open_date",
    "close_date",
    "issue_price",
    "exclude",
    "yf_ticker",
    "rhp_path",
    "drhp_path",
}


@dataclass
class Overrides:
    by_symbol: dict[str, dict[str, str]] = field(default_factory=dict)

    def get(self, symbol: str, name: str) -> str | None:
        return self.by_symbol.get(symbol, {}).get(name)

    def excluded(self, symbol: str) -> bool:
        return self.get(symbol, "exclude") is not None

    def apply(self, issue: NseIssue) -> NseIssue:
        values = self.by_symbol.get(issue.symbol)
        if not values:
            return issue
        changes: dict[str, object] = {}
        for name in ("listing_date", "open_date", "close_date"):
            if name in values:
                changes[name] = date.fromisoformat(values[name])
        if "issue_price" in values:
            changes["issue_price"] = Decimal(values["issue_price"])
        return replace(issue, **changes)  # type: ignore[arg-type]


def parse_overrides(text: str) -> Overrides:
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        return Overrides()
    if header != _HEADER:
        raise ValueError(f"overrides.csv header must be {_HEADER}, got {header!r}")
    out = Overrides()
    for line_no, row in enumerate(reader, start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != len(_HEADER):
            raise ValueError(f"overrides.csv line {line_no}: expected 4 columns, got {row!r}")
        symbol, name, value, _note = (cell.strip() for cell in row)
        if name not in _FIELDS:
            raise ValueError(f"overrides.csv line {line_no}: unknown field {name!r}")
        out.by_symbol.setdefault(symbol, {})[name] = value
    return out


def load_overrides(path: Path = DEFAULT_PATH) -> Overrides:
    return parse_overrides(path.read_text(encoding="utf-8")) if path.exists() else Overrides()
