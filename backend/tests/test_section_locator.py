from dataclasses import dataclass

import pytest

from app.parsing.section_locator import (
    SECTION_KEYS,
    locate_sections,
    parse_toc,
    printed_to_pdf_map,
)


@dataclass
class P:
    page_no: int
    text: str
    printed_no: int | None = None


def toc_page(n: int, lines: list[str]) -> P:
    return P(n, "\n".join(["TABLE OF CONTENTS", *lines]))


def test_parse_toc_handles_leader_styles_wraps_and_prefixes() -> None:
    page = toc_page(
        3,
        [
            "SECTION I: GENERAL .......................... 1",
            "CERTAIN CONVENTIONS, PRESENTATION OF FINANCIAL, INDUSTRY AND MARKET DATA AND",
            "CURRENCY OF PRESENTATION ......... 23",
            "SECTION II – RISK FACTORS………………...……… 30",
            "BASIS FOR OFFER PRICE … 99",
            "CAPITAL STRUCTURE   76",
            "MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS",
            "…  250",
            "OBJECTS OF THE OFFER…………………………",
            "88",
            "(i)",
        ],
    )
    entries = parse_toc([P(1, "cover page: the contents of this document"), P(2, "x"), page])
    assert [(e.title, e.printed_page, e.is_section_header) for e in entries] == [
        ("GENERAL", 1, True),
        (
            "CERTAIN CONVENTIONS PRESENTATION OF FINANCIAL INDUSTRY AND MARKET DATA AND CURRENCY OF PRESENTATION",
            23,
            False,
        ),
        ("RISK FACTORS", 30, True),
        ("BASIS FOR OFFER PRICE", 99, False),
        ("CAPITAL STRUCTURE", 76, False),
        (
            "MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS",
            250,
            False,
        ),
        ("OBJECTS OF THE OFFER", 88, False),
    ]


def test_parse_toc_follows_continuation_page_and_stops() -> None:
    pages = [
        toc_page(1, ["RISK FACTORS ..... 10"]),
        P(2, "CAPITAL STRUCTURE ..... 20\nOUR PROMOTERS AND PROMOTER GROUP ..... 30"),
        P(3, "Definitions and abbreviations\nThe Company means ..... and 12"),
    ]
    titles = [e.title for e in parse_toc(pages)]
    assert titles[:3] == ["RISK FACTORS", "CAPITAL STRUCTURE", "OUR PROMOTERS AND PROMOTER GROUP"]


def test_no_toc_means_no_entries() -> None:
    assert parse_toc([P(1, "no contents here"), P(2, "RISK FACTORS ..... 10")]) == []


def test_printed_to_pdf_map_handles_changing_offset() -> None:
    # Offset 1 for two pages, then an unnumbered insert and offset 2 for three pages.
    pages = [P(1, ""), P(2, "", 1), P(3, "", 2), P(4, ""), P(5, "", 3), P(6, "", 4), P(7, "", 5)]
    mapping, offset = printed_to_pdf_map(pages)
    assert mapping == {1: 2, 2: 3, 3: 5, 4: 6, 5: 7}
    assert offset == 2  # most common


# --- a synthetic offer document -------------------------------------------------------

TOC = [
    "SECTION I: GENERAL ..... 1",
    "SECTION II: RISK FACTORS ..... 2",
    "CAPITAL STRUCTURE ..... 4",
    "OBJECTS OF THE OFFER ..... 5",
    "BASIS FOR OFFER PRICE ..... 6",
    "OUR PROMOTERS AND PROMOTER GROUP ..... 7",
    "OUR GROUP ENTITIES ..... 8",
    "SECTION V: FINANCIAL INFORMATION ..... 9",
    "RESTATED CONSOLIDATED FINANCIAL INFORMATION ..... 9",
    "OTHER FINANCIAL INFORMATION ..... 14",
    "RELATED PARTY TRANSACTIONS ..... 15",
    "FINANCIAL INDEBTEDNESS ..... 16",
    "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS ..... 17",
    "GOVERNMENT AND OTHER APPROVALS ..... 19",
]
BODY = {  # printed page -> text (PDF page = printed + 2: cover and TOC are unnumbered)
    1: "SECTION I: GENERAL\nDEFINITIONS",
    2: "SECTION II: RISK FACTORS\nRISK FACTORS\nInternal risks",
    3: "more risks",
    4: "CAPITAL STRUCTURE\nShare capital",
    5: "OBJECTS OF THE OFFER\nNet proceeds",
    6: "BASIS FOR OFFER PRICE\nEPS",
    7: "OUR PROMOTERS AND PROMOTER GROUP\nPromoters",
    8: "OUR GROUP ENTITIES\nGroup companies",
    9: "SECTION V: FINANCIAL INFORMATION\nRESTATED CONSOLIDATED FINANCIAL INFORMATION",
    10: "Balance sheet",
    11: "Notes forming part of the restated financial information\nOther notes\n"
    + "x\n" * 20
    + "G. Related party transactions have been disclosed in accordance with Ind AS 24",
    12: "Notes\n(b)Related Party transactions details:\nDirector remuneration",
    13: "Notes\nBalances with related parties\nH. Fair values",
    14: "OTHER FINANCIAL INFORMATION\nRatios",
    15: "RELATED PARTY TRANSACTIONS\nFor details, see Note 35 Related Party Disclosures on page 11.",
    16: "FINANCIAL INDEBTEDNESS\nBorrowings",
    17: "Continuation of the previous chapter",  # TOC is one page early here
    18: "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS\nCriminal proceedings",
    19: "Litigation continued",
    20: "GOVERNMENT AND OTHER APPROVALS\nLicences",
    21: "Approvals continued",
}


def offer_document() -> list[P]:
    pages = [P(1, "Cover page\nRead the contents carefully"), toc_page(2, TOC)]
    for printed, text in BODY.items():
        pages.append(P(printed + 2, text, printed))
    return pages


def test_locate_sections_end_to_end() -> None:
    result = locate_sections(offer_document())
    assert result.missing == []
    assert result.page_offset == 2
    sm = result.section_map
    assert set(sm) == set(SECTION_KEYS)
    assert sm["risk_factors"] == [4, 5]
    assert sm["capital_structure"] == [6, 6]
    assert sm["objects_of_issue"] == [7, 7]
    assert sm["basis_for_issue_price"] == [8, 8]
    assert sm["promoters"] == [9, 9]
    assert sm["group_companies"] == [10, 10]  # "Our Group Entities"
    assert sm["restated_financials"] == [11, 15]
    # Printed 17 continues financial indebtedness: the TOC puts litigation one page early.
    assert sm["financial_indebtedness"] == [18, 19]
    assert sm["outstanding_litigation"] == [20, 21]


def test_heading_one_page_off_is_adjusted_and_end_confirmed() -> None:
    hit = locate_sections(offer_document()).hits["outstanding_litigation"]
    assert hit.method == "toc+adjusted"  # TOC says printed 17, heading is on printed 18
    assert hit.heading_found and hit.end_confirmed
    assert locate_sections(offer_document()).hits["financial_indebtedness"].end_confirmed


def test_related_party_cross_reference_is_followed_to_the_note() -> None:
    hit = locate_sections(offer_document()).hits["related_party"]
    # The TOC chapter only points to the note; the note starts mid-page on printed 11
    # ("G. Related party ...") and continues while pages discuss related parties.
    assert hit.method == "toc->note"
    assert (hit.start, hit.end) == (13, 15)


def test_related_party_found_by_search_when_not_in_toc() -> None:
    pages = offer_document()
    pages[1] = toc_page(2, [line for line in TOC if "RELATED PARTY" not in line])
    hit = locate_sections(pages).hits["related_party"]
    assert hit.method == "search"
    assert hit.start == 13


def test_passing_mention_is_not_a_related_party_heading() -> None:
    pages = offer_document()
    pages[1] = toc_page(2, [line for line in TOC if "RELATED PARTY" not in line])
    for p in pages:
        if p.printed_no in (11, 12, 13):
            p.text = (
                "Notes\n"
                + "x\n" * 20
                + "(viii) Details of related party transactions with respect to CSR"
            )
    hit = locate_sections(pages).hits["related_party"]
    assert hit.method == "search"  # only weak (mid-page) candidates remain, earliest wins
    assert hit.start == 13


def test_missing_sections_are_reported() -> None:
    pages = offer_document()
    pages[1] = toc_page(2, [line for line in TOC if "GROUP ENTITIES" not in line])
    result = locate_sections(pages)
    assert "group_companies" in result.missing
    assert "group_companies" not in result.section_map


def test_no_toc_finds_nothing_but_does_not_crash() -> None:
    result = locate_sections([P(1, "just text"), P(2, "more text", 1)])
    assert result.section_map == {}
    assert set(result.missing) == set(SECTION_KEYS)


def test_unconfirmed_end_is_flagged() -> None:
    pages = offer_document()
    for p in pages:
        if p.printed_no == 14:
            p.text = "Still restated financial statements"  # next heading missing
    hit = locate_sections(pages).hits["restated_financials"]
    assert not hit.end_confirmed


@pytest.mark.parametrize("rank_of_section_chunk", [1, 10, 11])
def test_embedding_rank_confirmation(rank_of_section_chunk: int) -> None:
    def search(query: str, k: int) -> list[tuple[int, int, float]]:
        others = [(100 + i, 100 + i, 0.9 - i * 0.01) for i in range(rank_of_section_chunk - 1)]
        return [*others, (6, 6, 0.5), (200, 200, 0.1)]

    hit = locate_sections(offer_document(), search=search).hits["capital_structure"]
    assert hit.embedding_rank == rank_of_section_chunk
    assert hit.embedding_score == 0.5
    assert hit.embedding_confirmed is (rank_of_section_chunk <= 10)


def test_heading_check_tolerates_small_wording_differences() -> None:
    pages = offer_document()
    for p in pages:
        if p.printed_no == 5:
            p.text = "OBJECT OF THE OFFER\nNet proceeds"  # singular on the page
    hit = locate_sections(pages).hits["objects_of_issue"]
    assert hit.heading_found and hit.method == "toc" and (hit.start, hit.end) == (7, 7)


def test_unrelated_heading_is_not_accepted() -> None:
    pages = offer_document()
    for p in pages:
        if p.printed_no == 4:
            p.text = "SHARE CAPITAL HISTORY\nDetails"
    hit = locate_sections(pages).hits["capital_structure"]
    assert not hit.heading_found
