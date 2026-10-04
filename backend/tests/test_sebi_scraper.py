from datetime import date
from pathlib import Path

import httpx
import pytest

from app.ingest.exchange_data import NseIssue
from app.ingest.http import PoliteClient
from app.ingest.sebi_scraper import (
    classify_slug,
    extract_pdf_urls,
    match_filings,
    name_key,
    parse_index,
    slug_company_key,
    store_pdf,
)
from tests.helpers import make_pdf


def issue(name: str, open_day: date, symbol: str = "X") -> NseIssue:
    return NseIssue(
        company=name,
        security_type="EQ",
        symbol=symbol,
        issue_price=None,
        open_date=open_day,
        close_date=None,
        listing_date=None,
        price_range="",
    )


@pytest.mark.parametrize(
    ("slug", "kind"),
    [
        ("zomato-limited-rhp", "main"),
        ("a-one-steel-limited", "main"),
        ("swiggy-limited-updated-drhp-i", "main"),
        ("zomato-limited-addendum-to-rhp", "addendum"),
        ("cmr-green-technologies-limited-addendum-cum-corrigendum", "addendum"),
        ("corrigendtum-to-rhp-mstc-limited", "corrigendum"),
        ("ideaforge-technology-limited-corigendum-to-rhp", "corrigendum"),
        ("mstc-ltd-price-band-revision-advertiement", "other"),
        ("easing-of-operational-procedure", "other"),
        ("v-marc-india-limited-sme-ipo-rhp", "sme"),
    ],
)
def test_classify_slug(slug: str, kind: str) -> None:
    assert classify_slug(slug) == kind


@pytest.mark.parametrize(
    ("company", "slug"),
    [
        ("Zomato Limited", "zomato-limited-rhp"),
        ("Dr. Agarwal's Health Care Limited", "dr-agarwal-s-healthcare-limited-rhp"),
        ("Orient Cables (India) Limited", "orient-cables-limited-drhp"),
        ("Sterling & Wilson Solar Limited", "red-herring-prospectus-of-sterling-and-wilson-solar-limited"),
        ("K.P.R. Agrochem Limited", "k-p-r-agrochem-limited"),
        ("Kronox Lab SciencesLimited", "kronox-lab-sciences-limited-rhp"),
        ("RailTel Corporation of India Limited", "railtel-corporation-of-india-limited-draft-red-herring-prospectus"),
    ],
)  # fmt: skip
def test_company_and_slug_keys_agree(company: str, slug: str) -> None:
    assert name_key(company) == slug_company_key(slug)


def test_parse_index_rejects_bad_lines() -> None:
    with pytest.raises(ValueError, match="line 2"):
        parse_index("2024-01-01|jan-2024/a-limited_1.html\nnot-a-date|x\n")


def test_parse_index_builds_urls_and_kinds() -> None:
    (f,) = parse_index("2021-07-08|jul-2021/zomato-limited-rhp_50950.html\n\n")
    assert f.filed_on == date(2021, 7, 8)
    assert (
        f.url
        == "https://www.sebi.gov.in/filings/public-issues/jul-2021/zomato-limited-rhp_50950.html"
    )
    assert f.slug == "zomato-limited-rhp"
    assert f.kind == "main"


INDEX_RHP = parse_index(
    "\n".join(
        [
            "2020-03-03|mar-2020/antony-waste-handling-cell-limited_1.html",  # withdrawn attempt
            "2020-12-16|dec-2020/antony-waste-handling-cell-ltd-_2.html",
            "2020-12-21|dec-2020/corrigendum-to-the-rhp-of-antony-waste-handling-cell-ltd-_3.html",
            "2023-09-06|sep-2023/samhi-hotels-limited-rhp_4.html",
            "2023-09-11|sep-2023/samhi-hotels-limited-rhp_5.html",
            "2025-07-09|jul-2025/crizac-limited-rhp_6.html",  # posted after the issue opened
            "2025-07-09|jul-2025/crizac-pharma-limited-rhp_7.html",
        ]
    )
)
INDEX_DRHP = parse_index(
    "\n".join(
        [
            "2019-01-02|jan-2019/antony-waste-handling-cell-limited_10.html",
            "2020-09-29|sep-2020/antony-waste-handling-cell-ltd-_11.html",
            "2016-01-01|jan-2016/antony-waste-handling-cell-limited_12.html",  # too old
            "2024-11-19|nov-2024/crizac-limited-drhp_13.html",
            "2025-07-30|jul-2025/crizac-limited-drhp_14.html",  # after the issue: not a DRHP for it
        ]
    )
)


def test_match_picks_filing_for_this_issue_not_withdrawn_attempt() -> None:
    m = match_filings(
        issue("Antony Waste Handling Cell Limited", date(2020, 12, 21)), INDEX_RHP, INDEX_DRHP
    )
    assert m.rhp is not None and m.rhp.path.endswith("_2.html")
    assert m.drhp is not None and m.drhp.path.endswith("_11.html")  # latest before the issue
    assert m.rhp_score == 100.0


def test_match_prefers_latest_rhp_before_open() -> None:
    m = match_filings(issue("SAMHI Hotels Limited", date(2023, 9, 14)), INDEX_RHP, INDEX_DRHP)
    assert m.rhp is not None and m.rhp.path.endswith("_5.html")


def test_match_accepts_rhp_posted_after_open_and_rejects_similar_names() -> None:
    m = match_filings(issue("CRIZAC LIMITED", date(2025, 7, 2)), INDEX_RHP, INDEX_DRHP)
    assert m.rhp is not None and m.rhp.path.endswith("_6.html")
    assert m.drhp is not None and m.drhp.path.endswith("_13.html")


def test_no_match_below_threshold_or_outside_window() -> None:
    m = match_filings(issue("Totally Different Limited", date(2023, 9, 14)), INDEX_RHP, INDEX_DRHP)
    assert m.rhp is None and m.drhp is None
    far = match_filings(issue("SAMHI Hotels Limited", date(2026, 9, 14)), INDEX_RHP, INDEX_DRHP)
    assert far.rhp is None


def test_match_without_any_date_returns_nothing() -> None:
    bare = NseIssue("Zomato Limited", "EQ", "ZOMATO", None, None, None, None, "")
    m = match_filings(bare, INDEX_RHP, INDEX_DRHP)
    assert m.rhp is None and m.drhp is None


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("../../../web/?file=/sebi_data/attachdocs/apr-2019/1554987434164.pdf",
         "https://www.sebi.gov.in/sebi_data/attachdocs/apr-2019/1554987434164.pdf"),
        ("../../../web/?file=https://www.sebi.gov.in/sebi_data/attachdocs/oct-2024/1730179002038.pdf",
         "https://www.sebi.gov.in/sebi_data/attachdocs/oct-2024/1730179002038.pdf"),
    ],
)  # fmt: skip
def test_extract_pdf_urls_from_iframe(src: str, expected: str) -> None:
    page = "https://www.sebi.gov.in/filings/public-issues/apr-2019/polycab-india-limited_42693.html"
    html = f"<html><body><iframe src='{src}' width='100%'></iframe></body></html>"
    assert extract_pdf_urls(html, page) == [expected]


def test_extract_pdf_urls_ignores_non_pdf_and_dedupes() -> None:
    page = "https://www.sebi.gov.in/filings/public-issues/x/y_1.html"
    html = (
        "<a href='/index.html'>home</a>"
        "<iframe src='../../../web/?file=/sebi_data/a.pdf'></iframe>"
        "<a href='https://www.sebi.gov.in/sebi_data/a.pdf'>download</a>"
    )
    assert extract_pdf_urls(html, page) == ["https://www.sebi.gov.in/sebi_data/a.pdf"]


def client_for(handler: httpx.MockTransport, tmp_path: Path) -> PoliteClient:
    return PoliteClient(
        tmp_path / "cache", "test-agent", min_interval_s=0, transport=handler, sleep=lambda s: None
    )


def test_store_pdf_names_file_by_sha_and_counts_pages(tmp_path: Path) -> None:
    pdf = make_pdf(3)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=pdf)

    with client_for(httpx.MockTransport(handler), tmp_path) as client:
        first = store_pdf(client, "https://example.test/doc.pdf", tmp_path / "pdfs")
        again = store_pdf(client, "https://example.test/copy.pdf", tmp_path / "pdfs")
    assert first.n_pages == 3
    assert first.storage_path == tmp_path / "pdfs" / f"{first.sha256}.pdf"
    assert again.sha256 == first.sha256  # identical bytes stored once
    assert sorted(p.name for p in (tmp_path / "pdfs").iterdir()) == [f"{first.sha256}.pdf"]


def test_store_pdf_rejects_non_pdf(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"<html>not a pdf</html>")

    with client_for(httpx.MockTransport(handler), tmp_path) as client:
        with pytest.raises(ValueError, match="did not return a PDF"):
            store_pdf(client, "https://example.test/doc.pdf", tmp_path / "pdfs")
    assert list((tmp_path / "pdfs").iterdir()) == []
