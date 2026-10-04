from datetime import date
from decimal import Decimal

import pytest

from app.ingest.exchange_data import (
    exclusion_reason,
    parse_past_issues,
    select_universe,
)

HEADER = (
    '"COMPANY NAME","SECURITY TYPE","ISSUE PRICE","Symbol","ISSUE START DATE",'
    '"ISSUE END DATE","PRICE RANGE","DATE OF LISTING"'
)


def csv_text(*rows: str, bom: bool = True) -> str:
    return ("﻿" if bom else "") + "\n".join([HEADER, *rows]) + "\n"


ROWS = [
    # Mainboard EQ with NSE's left-padded price.
    '"Alpha Limited","EQ","   114","ALPHA","22-DEC-2025","24-DEC-2025","Rs.108 to Rs.114","30-DEC-2025"',
    # Trade-for-trade mainboard series.
    '"Beta Limited","BE","   100","BETA","30-SEP-2025","03-OCT-2025","Rs.95 to Rs.100","08-OCT-2025"',
    '"Gamma Limited","SME","126","GAMMA","23-DEC-2025","26-DEC-2025","RS.120 to RS.126","31-DEC-2025"',
    '"Muni Corp","DEBT","-","MUNI01","06-OCT-2025","09-OCT-2025","Rs.1000","14-OCT-2025"',
    '"Insure Co","N0","######","815INS36","10-OCT-2025","14-OCT-2025","Rs.100","17-MAR-2026"',
    '"Delta Limited - FPO","EQ","-","DELTAFPO","18-APR-2024","22-APR-2024","Rs.10 to Rs.11","25-APR-2024"',
    '"Epsilon Limited","EQ","450","EPSPP1","09-JUL-2025","15-JUL-2025","Rs.1000","10-FEB-2026"',
    '"Zeta Limited","EQ","34","ZETA","24-MAR-2022","28-MAR-2022","Rs.615 to Rs.650","02-JAN-2003"',
    '"Eta Limited","EQ","-","ETA","28-JUN-2019","02-JUL-2019","Rs 59 to Rs 61","-"',
    '"Theta Limited","EQ","315","THETA","21-DEC-2020","23-DEC-2020","Rs.313 to Rs.315","01-JAN-2021"',
    '"Theta Limited - Issue Withdrawn","EQ","-","THETA","04-MAR-2020","06-MAR-2020","Rs.294","-"',
    # Exact duplicate row, as NSE's file contains.
    '"Alpha Limited","EQ","   114","ALPHA","22-DEC-2025","24-DEC-2025","Rs.108 to Rs.114","30-DEC-2025"',
]


def test_parse_handles_bom_padding_and_placeholders() -> None:
    issues = parse_past_issues(csv_text(*ROWS))
    assert len(issues) == len(ROWS)
    alpha = issues[0]
    assert alpha.company == "Alpha Limited"
    assert alpha.issue_price == Decimal("114")
    assert alpha.open_date == date(2025, 12, 22)
    assert alpha.close_date == date(2025, 12, 24)
    assert alpha.listing_date == date(2025, 12, 30)
    insure = next(i for i in issues if i.symbol == "815INS36")
    assert insure.issue_price is None
    eta = next(i for i in issues if i.symbol == "ETA")
    assert eta.issue_price is None and eta.listing_date is None


def test_parse_without_bom() -> None:
    assert len(parse_past_issues(csv_text(ROWS[0], bom=False))) == 1


def test_parse_rejects_unexpected_header() -> None:
    with pytest.raises(ValueError, match="header"):
        parse_past_issues('"NAME","TYPE"\n"a","b"\n')


def test_parse_rejects_malformed_row() -> None:
    with pytest.raises(ValueError, match="Malformed"):
        parse_past_issues(csv_text('"Only","three","cols"'))


def test_select_universe_keeps_listed_mainboard_ipos_only() -> None:
    universe, excluded = select_universe(parse_past_issues(csv_text(*ROWS)))
    assert [i.symbol for i in universe] == ["ALPHA", "BETA", "THETA"]
    reasons = {(i.symbol, r) for i, r in excluded}
    assert ("DELTAFPO", "follow-on offer (FPO)") in reasons
    assert ("EPSPP1", "partly-paid shares") in reasons
    assert ("ZETA", "listed before issue opened (follow-on offer)") in reasons
    assert ("ETA", "no listing date in NSE file") in reasons
    assert ("THETA", "issue withdrawn") in reasons
    assert ("ALPHA", "duplicate symbol") in reasons
    # SME, debt and other security types are never considered.
    assert not {"GAMMA", "MUNI01", "815INS36"} & {i.symbol for i, _ in excluded}


def test_relisted_company_keeps_the_listed_attempt() -> None:
    universe, _ = select_universe(parse_past_issues(csv_text(*ROWS)))
    theta = next(i for i in universe if i.symbol == "THETA")
    assert theta.listing_date == date(2021, 1, 1)


def test_partly_paid_rule_does_not_catch_ordinary_symbols() -> None:
    issues = parse_past_issues(
        csv_text(
            '"Shriram Properties Limited","EQ","118","SHRIRAMPPS","08-DEC-2021",'
            '"10-DEC-2021","Rs.113 to Rs.118","20-DEC-2021"'
        )
    )
    assert exclusion_reason(issues[0]) is None
