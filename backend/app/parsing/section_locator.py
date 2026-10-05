"""Find the key sections of an offer document (build guide Phase 2 step 5).

1. Parse the document's own table of contents (printed page numbers).
2. Map printed page numbers to PDF page numbers using the page numbers printed in each
   page's footer.
3. Confirm each section by its heading on the start page (keyword check, adjusting a few
   pages if needed) and by embedding search over the document's chunks.
4. Sections missing from the table of contents (commonly related-party transactions, which
   are usually a note inside the restated financial information) are found by heading
   keyword search ranked by embedding similarity.

Output (step 6): {"related_party": [312, 318], ...} in 1-based PDF page numbers.
"""

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from rapidfuzz import fuzz

# Section keys (build guide Phase 2 step 5; promoters and group companies are separate
# chapters in offer documents, so they get separate keys).
SECTION_KEYS = (
    "restated_financials",
    "related_party",
    "outstanding_litigation",
    "capital_structure",
    "objects_of_issue",
    "risk_factors",
    "promoters",
    "group_companies",
    "basis_for_issue_price",
    "financial_indebtedness",
)

# Table-of-contents title patterns, matched against the normalised title (uppercase,
# single spaces, "SECTION X:" prefix removed).
_TOC_PATTERNS: dict[str, re.Pattern[str]] = {
    "risk_factors": re.compile(r"^RISK FACTORS$"),
    "capital_structure": re.compile(r"^CAPITAL STRUCTURE$"),
    "objects_of_issue": re.compile(r"^OBJECTS? OF THE (ISSUE|OFFER)$"),
    "basis_for_issue_price": re.compile(r"^BASIS (FOR|OF) (THE )?(ISSUE|OFFER) PRICE$"),
    "promoters": re.compile(r"^OUR PROMOTERS?( AND (THE )?PROMOTER GROUP)?$"),
    "group_companies": re.compile(r"^(OUR )?GROUP (COMPAN(Y|IES)|ENTITIES)$"),
    "related_party": re.compile(r"^(STATEMENT OF )?RELATED PARTY TRANSACTIONS$"),
    "financial_indebtedness": re.compile(r"^FINANCIAL INDEBTEDNESS$"),
    "outstanding_litigation": re.compile(r"^OUTSTANDING LITIGATIONS?\b"),
}
# Restated financials: prefer an explicit "RESTATED ... FINANCIAL ..." entry, else a plain
# "FINANCIAL STATEMENTS/INFORMATION" sub-entry, else the section header itself.
_RESTATED = re.compile(r"\bRESTATED\b.*\bFINANCIAL (INFORMATION|STATEMENTS)\b")
_PLAIN_FINANCIALS = re.compile(r"^(STANDALONE |CONSOLIDATED )?FINANCIAL (INFORMATION|STATEMENTS)$")
_NOT_RESTATED = re.compile(r"\b(SUMMARY|OTHER|PRO ?FORMA|AUDITED)\b")

# Semantic queries for embedding confirmation and fallback search.
SECTION_QUERIES = {
    "restated_financials": (
        "restated financial information: restated statement of assets and liabilities, "
        "profit and loss, cash flows and notes"
    ),
    "related_party": (
        "related party transactions: names of related parties, nature of relationship, "
        "and transactions with key managerial personnel and promoters"
    ),
    "outstanding_litigation": (
        "outstanding litigation and material developments: criminal proceedings, actions "
        "by regulatory authorities, tax claims against the company, promoters and "
        "directors"
    ),
    "capital_structure": (
        "capital structure: authorised, issued and paid-up share capital, share capital "
        "history, shareholding of promoters and pledge of shares"
    ),
    "objects_of_issue": (
        "objects of the offer: utilisation of net proceeds, repayment of borrowings, "
        "general corporate purposes"
    ),
    "risk_factors": (
        "risk factors: internal risks to our business, financial condition and results of"
        " operations"
    ),
    "promoters": (
        "our promoters and promoter group: details of promoters, interests of promoters, "
        "payment of benefits"
    ),
    "group_companies": (
        "our group companies: details of group companies, litigation and common pursuits"
    ),
    "basis_for_issue_price": (
        "basis for offer price: qualitative and quantitative factors, earnings per share,"
        " price to earnings ratio, key performance indicators"
    ),
    "financial_indebtedness": (
        "financial indebtedness: secured and unsecured borrowings, sanctioned amounts, "
        "principal terms of loans"
    ),
}

_TOC_HEADING = re.compile(r"\b(TABLE OF )?CONTENTS\b")
# Dot leaders may mix "." and "…" ("GENERAL……………...... 1") or be plain spacing.
_LEADER = r"(?:[.…\s]*(?:…|\.\.)[.…\s]*|\s{2,})"
_TOC_LINE = re.compile(rf"^(?P<title>.*?){_LEADER}(?P<page>\d{{1,4}})\s*$")
_TRAILING_LEADER = re.compile(r"^(?P<title>.*?[A-Za-z)].*?)[.…\s]*(?:…|\.\.)[.…\s]*$")
_NUMBER_ONLY = re.compile(r"^\d{1,4}$")
_LEADER_ONLY = re.compile(r"^[.…\s]*(?:…|\.\.)[.…\s]*(?P<page>\d{1,4})\s*$")
_SECTION_PREFIX = re.compile(r"^SECTION\s+[IVXLC]+\s*[:\-–—.]?\s*")
# A short heading line such as "Note 36: Related party disclosures", "Other Related party
# disclosures", "Related Parties and transactions" or "Restated Consolidated Statement of
# Transactions with Related Parties" (not "Receivable from related parties").
_RPT_HEADING = re.compile(
    r"^\s*(?:[\w()\-–—]+(?:\s*[:.]\s*|\s+)){0,5}(?:"
    r"related part(?:y|ies)(?:\s+and)?\s+(?:transactions?|disclosures?)"
    r"|transactions?(?:\s+and\s+balances)?\s+with\s+related\s+part(?:y|ies)"
    r"|list\s+of\s+related\s+part(?:y|ies)"
    r"|names?\s+of\s+(?:the\s+)?related\s+part(?:y|ies)"
    r")\b",
    re.IGNORECASE,
)
_RPT_HEADING_MAX_CHARS = 100
# Enumerators in front of note titles: "G.", "(b)", "4)", "A:".
_ENUMERATOR = re.compile(r"^\s*(?:\(?[A-Za-z0-9]{1,3}[).:]\s*)+")
HEADING_LINES = 12  # a heading must appear in the first lines of its start page
HEADING_MATCH = 92  # rapidfuzz partial_ratio for a heading with small wording differences
ADJUST_WINDOW = 3  # pages either side of the TOC page to look for a missing heading
RPT_MAX_PAGES = 20
ALL_CHUNKS = 100_000  # rank against every chunk of the document
# The section is embedding-confirmed when one of its chunks is among the document's
# top-N chunks for the section query.
EMBEDDING_CONFIRM_TOP = 10


class PageLike(Protocol):
    page_no: int
    text: str
    printed_no: int | None


# (query, k) -> [(page_start, page_end, similarity), ...] best first
SemanticSearch = Callable[[str, int], list[tuple[int, int, float]]]


@dataclass(frozen=True)
class TocEntry:
    title: str  # normalised, without "SECTION X:" prefix
    printed_page: int
    is_section_header: bool


@dataclass
class SectionHit:
    key: str
    start: int
    end: int
    method: str  # "toc", "toc+adjusted", "search"
    heading_found: bool
    toc_title: str | None = None
    embedding_score: float | None = None
    embedding_rank: int | None = None
    end_confirmed: bool = True  # the page after `end` starts the following section

    @property
    def embedding_confirmed(self) -> bool | None:
        if self.embedding_rank is None:
            return None
        return self.embedding_rank <= EMBEDDING_CONFIRM_TOP


@dataclass
class LocatorResult:
    section_map: dict[str, list[int]] = field(default_factory=dict)
    hits: dict[str, SectionHit] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    toc_entries: list[TocEntry] = field(default_factory=list)
    page_offset: int | None = None


def _normalise(text: str) -> str:
    text = text.upper().replace("’", "'").replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9' ]+", " ", text)
    text = re.sub(r"ISATION\b", "IZATION", text)  # British and American spellings
    return re.sub(r"\s+", " ", text).strip()


def parse_toc(pages: Sequence[PageLike], scan_pages: int = 15) -> list[TocEntry]:
    """Entries from the printed table of contents in the first `scan_pages` pages; a TOC
    continuing onto following pages is followed until entries stop."""
    entries: list[TocEntry] = []
    started = False
    for page in pages[:scan_pages]:
        lines = page.text.splitlines()
        if not started and not any(_TOC_HEADING.fullmatch(ln.strip().upper()) for ln in lines):
            continue  # body text mentioning "contents" is not the table of contents
        page_entries = _parse_toc_lines(lines)
        if not page_entries:
            if started:
                break
            continue
        started = True
        entries.extend(page_entries)
    return entries


def _parse_toc_lines(lines: list[str]) -> list[TocEntry]:
    entries = []
    pending = ""  # title text waiting for its page number
    awaiting_number = False  # pending title ended in a leader; the number is on the next line
    for raw in lines:
        line = raw.strip()
        if not line or _TOC_HEADING.fullmatch(line.upper()):
            continue
        leader = _LEADER_ONLY.match(line)
        match = None if leader else _TOC_LINE.match(line)
        number_only = _NUMBER_ONLY.match(line)
        if awaiting_number and number_only:
            title, page = pending, int(line)
        elif leader and pending:
            title, page = pending, int(leader.group("page"))
        elif match and re.search(r"[A-Za-z]", match.group("title")):
            title, page = f"{pending} {match.group('title')}".strip(), int(match.group("page"))
        else:
            trailing = _TRAILING_LEADER.match(line)
            if trailing:  # "TITLE………" with the page number on the next line
                pending = f"{pending} {trailing.group('title')}".strip()
                awaiting_number = True
            elif re.search(r"[A-Za-z]{3}", line):  # wrapped title
                pending = f"{pending} {line}".strip()
                awaiting_number = False
            else:  # noise such as "(i)"
                pending, awaiting_number = "", False
            continue
        pending, awaiting_number = "", False
        upper = title.upper()
        is_header = bool(_SECTION_PREFIX.match(upper))
        clean = _normalise(_SECTION_PREFIX.sub("", upper))
        if clean:
            entries.append(TocEntry(clean, page, is_header))
    return entries


def printed_to_pdf_map(pages: Sequence[PageLike]) -> tuple[dict[int, int], int | None]:
    """{printed page: PDF page} from footer numbers, and the most common offset."""
    mapping: dict[int, int] = {}
    offsets: Counter[int] = Counter()
    for page in pages:
        if page.printed_no is not None:
            mapping.setdefault(page.printed_no, page.page_no)
            offsets[page.page_no - page.printed_no] += 1
    offset = offsets.most_common(1)[0][0] if offsets else None
    return mapping, offset


def _to_pdf(printed: int, mapping: dict[int, int], offset: int | None, n_pages: int) -> int | None:
    pdf = mapping.get(printed)
    if pdf is None and offset is not None:
        pdf = printed + offset
    if pdf is None or not 1 <= pdf <= n_pages:
        return None
    return pdf


def _heading_on_page(page: PageLike, title: str) -> bool:
    """The title appears in the top lines, allowing small wording differences between the
    table of contents and the page ("OBJECTS OF THE OFFER" vs "OBJECT OF THE OFFER")."""
    head = _normalise(" ".join(page.text.splitlines()[:HEADING_LINES]))
    return title in head or fuzz.partial_ratio(title, head) >= HEADING_MATCH


def _pick_toc_entry(key: str, entries: list[TocEntry]) -> TocEntry | None:
    if key == "restated_financials":
        tests: list[Callable[[TocEntry], bool]] = [
            lambda e: bool(_RESTATED.search(e.title)) and not _NOT_RESTATED.search(e.title),
            lambda e: bool(_PLAIN_FINANCIALS.match(e.title)) and not e.is_section_header,
            lambda e: bool(_PLAIN_FINANCIALS.match(e.title)),
        ]
        for test in tests:
            for entry in entries:
                if test(entry):
                    return entry
        return None
    pattern = _TOC_PATTERNS[key]
    return next((e for e in entries if pattern.search(e.title)), None)


def _range_from_toc(
    entry: TocEntry,
    entries: list[TocEntry],
    mapping: dict[int, int],
    offset: int | None,
    n_pages: int,
) -> tuple[int, int] | None:
    start = _to_pdf(entry.printed_page, mapping, offset, n_pages)
    if start is None:
        return None
    later = [e.printed_page for e in entries if e.printed_page > entry.printed_page]
    if later:
        nxt = _to_pdf(min(later), mapping, offset, n_pages)
        end = (nxt - 1) if nxt is not None else n_pages
    else:
        end = n_pages
    return start, max(start, end)


def _embedding_check(
    key: str, start: int, end: int, search: SemanticSearch | None
) -> tuple[float | None, int | None]:
    """Similarity of the section query to the best chunk inside the range, and that
    chunk's rank (1 = best) among all of the document's chunks."""
    if search is None:
        return None, None
    ranked = search(SECTION_QUERIES[key], ALL_CHUNKS)
    for rank, (lo, hi, score) in enumerate(ranked, start=1):
        if lo <= end and hi >= start:
            return score, rank
    return None, None


def _find_related_party(
    pages: Sequence[PageLike], within: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Related-party transactions note: the first page with an RPT heading near the top
    (inside the restated financials when known; consolidated statements come before
    standalone ones), else the first page mentioning one; the range runs while consecutive
    pages keep discussing related parties. Embedding similarity confirms the choice."""
    by_no = {p.page_no: p for p in pages}
    lo, hi = within if within else (1, len(pages))

    def heading_rank(page: PageLike) -> int | None:
        """2: heading in the top lines (where note titles sit), 1: elsewhere, None: no heading."""
        best = None
        for i, line in enumerate(page.text.splitlines()):
            if len(line.strip()) <= _RPT_HEADING_MAX_CHARS and _RPT_HEADING.match(
                _ENUMERATOR.sub("", line)
            ):
                best = max(best or 0, 2 if i < HEADING_LINES else 1)
        return best

    ranks = {p.page_no: heading_rank(p) for p in pages if lo <= p.page_no <= hi}
    candidates = [n for n, rank in ranks.items() if rank]
    if not candidates:
        return None
    start = max(candidates, key=lambda n: (ranks[n] or 0, -n))
    # A note that begins lower down the previous page starts there.
    while ranks.get(start - 1):
        start -= 1
    end = start
    while (
        end + 1 in by_no
        and end + 1 <= hi
        and end - start < RPT_MAX_PAGES
        and "related part" in by_no[end + 1].text.lower()
    ):
        end += 1
    return start, end


def _next_entry(entry: TocEntry, entries: list[TocEntry]) -> TocEntry | None:
    """The first entry starting on a later printed page (the section that follows)."""
    later = [e for e in entries if e.printed_page > entry.printed_page]
    return min(later, key=lambda e: e.printed_page) if later else None


def _title_line_on_page(page: PageLike, title: str) -> bool:
    """The title appears as a line of its own anywhere on the page (mid-page headings)."""
    return any(_normalise(line) == title for line in page.text.splitlines())


CROSS_REFERENCE_MAX_CHARS = 2000
_ON_PAGE = re.compile(r"\bon\s+page\s+\d+", re.IGNORECASE)


def _is_cross_reference(by_no: dict[int, PageLike], start: int, end: int) -> bool:
    """A chapter that is only a short pointer to another part of the document."""
    body = " ".join(by_no[n].text for n in range(start, end + 1) if n in by_no)
    return len(body.strip()) < CROSS_REFERENCE_MAX_CHARS and bool(_ON_PAGE.search(body))


def _confirm_end(
    start: int, end: int, following: TocEntry | None, by_no: dict[int, PageLike]
) -> tuple[int, bool]:
    """Check that the page after `end` begins the following section; if not, look a few
    pages either way for its heading (at the top of a page, else mid-page)."""
    if following is None:
        return end, True
    nxt = by_no.get(end + 1)
    if nxt is None or _heading_on_page(nxt, following.title):
        return end, True
    deltas = [d for k in range(1, ADJUST_WINDOW + 1) for d in (k, -k)]
    for delta in deltas:
        page = by_no.get(end + 1 + delta)
        if page is not None and page.page_no > start and _heading_on_page(page, following.title):
            return page.page_no - 1, True
    for delta in deltas:
        page = by_no.get(end + 1 + delta)
        if page is not None and page.page_no > start and _title_line_on_page(page, following.title):
            return page.page_no, True  # the next section begins part-way down this page
    return end, False


def locate_sections(
    pages: Sequence[PageLike], search: SemanticSearch | None = None
) -> LocatorResult:
    result = LocatorResult()
    n_pages = len(pages)
    by_no = {p.page_no: p for p in pages}
    entries = parse_toc(pages)
    mapping, offset = printed_to_pdf_map(pages)
    result.toc_entries, result.page_offset = entries, offset

    for key in SECTION_KEYS:
        entry = _pick_toc_entry(key, entries)
        span = _range_from_toc(entry, entries, mapping, offset, n_pages) if entry else None
        if entry is not None and span is not None:
            start, end = span
            method = "toc"
            found = _heading_on_page(by_no[start], entry.title)
            if not found:
                for delta in (d for k in range(1, ADJUST_WINDOW + 1) for d in (k, -k)):
                    page = by_no.get(start + delta)
                    if page is not None and _heading_on_page(page, entry.title):
                        end = max(start + delta, end + delta)
                        start, method, found = start + delta, "toc+adjusted", True
                        break
            end, end_ok = _confirm_end(start, end, _next_entry(entry, entries), by_no)
            if key == "related_party" and _is_cross_reference(by_no, start, end):
                # The chapter only points to the note in the restated financials
                # ("... see Note 35 Related Party Disclosures on page 403"); use the note.
                fin = result.hits.get("restated_financials")
                note = _find_related_party(pages, (fin.start, fin.end) if fin else None)
                if note:
                    score, rank = _embedding_check(key, note[0], note[1], search)
                    result.hits[key] = SectionHit(
                        key, note[0], note[1], "toc->note", True, entry.title, score, rank
                    )
                    continue
            score, rank = _embedding_check(key, start, end, search)
            result.hits[key] = SectionHit(
                key, start, end, method, found, entry.title, score, rank, end_ok
            )
            continue
        if key == "related_party":
            fin = result.hits.get("restated_financials")
            span = _find_related_party(pages, (fin.start, fin.end) if fin else None)
            if span:
                score, rank = _embedding_check(key, span[0], span[1], search)
                result.hits[key] = SectionHit(
                    key, span[0], span[1], "search", True, None, score, rank
                )
                continue
        result.missing.append(key)

    result.section_map = {k: [h.start, h.end] for k, h in result.hits.items()}
    return result
