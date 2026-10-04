"""SEBI offer documents: filing indices, company matching, PDF download.

SEBI's listing pages (Filings -> Public Issues -> "Draft Offer Documents filed with SEBI"
and "Red Herring Documents filed with ROC") page through an endpoint that SEBI's firewall
blocks for scripts. The listings were therefore read in a browser and saved as indices
in data/raw/sebi/{rhp,drhp}_index.txt, one "YYYY-MM-DD|<month-folder>/<slug>_<id>.html"
line per filing (see README). Individual filing pages and PDFs are fetched here through
PoliteClient.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import pymupdf
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.ingest.exchange_data import NseIssue
from app.ingest.http import PoliteClient

SEBI_FILINGS_BASE = "https://www.sebi.gov.in/filings/public-issues/"

# Filing-date windows around the issue open date. The dates in SEBI's listing are when SEBI
# posted the document, which for RHPs is sometimes weeks after the issue opened.
RHP_BEFORE = timedelta(days=90)
RHP_AFTER = timedelta(days=120)
DRHP_WINDOW = timedelta(days=3 * 365)
MATCH_THRESHOLD = 90.0

_ADDENDUM = re.compile(r"\baddendum\b")
_CORRIGENDUM = re.compile(r"\b(corrigendum|corrigendtum|corigendum)\b")
_OTHER = re.compile(
    r"\b(price band|advertisement|advertiement|abridged|easing|ease of operational|"
    r"update in the objects)\b"
)
_SME = re.compile(r"\bsme\b")
_LIMITED = {"limited", "ltd", "llimited", "liited"}
# Words that describe the document rather than the company.
_DOC_WORDS = {
    "rhp", "drhp", "udrhp", "red", "herring", "hearing", "prospectus", "draft", "updated",
    "ipo", "addendum", "corrigendum", "corrigendtum", "corigendum", "cum", "to", "of", "the",
    "i", "ii", "1", "2", "2nd", "second",
}  # fmt: skip
# Dropped from both sides before comparing names.
_NAME_NOISE = _LIMITED | {"the", "and", "of", "india"}


@dataclass(frozen=True)
class SebiFiling:
    filed_on: date
    path: str  # "<month-folder>/<slug>_<id>.html"
    kind: str  # "main", "addendum", "corrigendum", "other", "sme"
    name_key: str

    @property
    def url(self) -> str:
        return SEBI_FILINGS_BASE + self.path

    @property
    def slug(self) -> str:
        return self.path.rsplit("/", 1)[-1].rsplit("_", 1)[0]


@dataclass(frozen=True)
class FilingMatch:
    rhp: SebiFiling | None
    drhp: SebiFiling | None
    rhp_score: float | None = None
    drhp_score: float | None = None


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("&", " and ")
    # Split a "limited"/"ltd" glued to the previous word ("SciencesLimited").
    text = re.sub(r"(?<=[a-z])(limited|ltd)\b", r" \1", text)
    return re.findall(r"[a-z0-9]+", text)


def name_key(company_name: str) -> str:
    """Compact comparison key for a company name ("Dr. Agarwal's Health Care Limited" ->
    "dragarwalshealthcare")."""
    return "".join(t for t in _tokens(company_name) if t not in _NAME_NOISE)


def classify_slug(slug: str) -> str:
    words = " ".join(_tokens(slug.replace("-", " ")))
    if _SME.search(words):
        return "sme"
    if _ADDENDUM.search(words):
        return "addendum"
    if _CORRIGENDUM.search(words):
        return "corrigendum"
    if _OTHER.search(words):
        return "other"
    return "main"


def slug_company_key(slug: str) -> str:
    """Company key from a filing slug: drop document words, keep up to "limited"."""
    tokens = _tokens(slug.replace("-", " "))
    last_limited = max((i for i, t in enumerate(tokens) if t in _LIMITED), default=None)
    if last_limited is not None:
        tokens = tokens[: last_limited + 1]
    tokens = [t for t in tokens if t not in _DOC_WORDS]
    return "".join(t for t in tokens if t not in _NAME_NOISE)


def parse_index(text: str) -> list[SebiFiling]:
    filings = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            day, path = line.split("|", 1)
            filed_on = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(f"bad SEBI index line {line_no}: {line!r}") from exc
        slug = path.rsplit("/", 1)[-1].rsplit("_", 1)[0]
        filings.append(
            SebiFiling(
                filed_on=filed_on,
                path=path,
                kind=classify_slug(slug),
                name_key=slug_company_key(slug),
            )
        )
    return filings


def load_index(path: Path) -> list[SebiFiling]:
    return parse_index(path.read_text(encoding="utf-8"))


def _best(
    key: str, candidates: list[SebiFiling], anchor: date
) -> tuple[SebiFiling | None, float | None]:
    """Highest name score; ties go to the filing posted nearest the anchor date, preferring
    one posted on or before it (the final version closest to the issue)."""

    def rank(filing: SebiFiling) -> tuple[float, int, int]:
        gap = (filing.filed_on - anchor).days
        return (float(fuzz.ratio(key, filing.name_key)), gap <= 0, -abs(gap))

    if not candidates:
        return None, None
    best = max(candidates, key=rank)
    score = rank(best)[0]
    if score < MATCH_THRESHOLD:
        return None, None
    return best, score


def match_filings(issue: NseIssue, rhps: list[SebiFiling], drhps: list[SebiFiling]) -> FilingMatch:
    """Pick the RHP and DRHP for one IPO by company name within filing-date windows."""
    key = name_key(issue.company)
    anchor = issue.open_date or issue.listing_date
    if anchor is None:
        return FilingMatch(None, None)
    rhp_pool = [
        f
        for f in rhps
        if f.kind == "main" and anchor - RHP_BEFORE <= f.filed_on <= anchor + RHP_AFTER
    ]
    rhp, rhp_score = _best(key, rhp_pool, anchor)
    drhp_pool = [
        f for f in drhps if f.kind == "main" and anchor - DRHP_WINDOW <= f.filed_on <= anchor
    ]
    drhp, drhp_score = _best(key, drhp_pool, anchor)
    return FilingMatch(rhp, drhp, rhp_score, drhp_score)


def extract_pdf_urls(html: str, page_url: str) -> list[str]:
    """PDF links on a SEBI filing page. The document is embedded as an iframe whose src is
    "../../../web/?file=<pdf url>" (relative or absolute); direct .pdf links also count."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for tag in soup.find_all(["iframe", "a", "embed"]):
        ref = tag.get("src") or tag.get("href")
        if not isinstance(ref, str):
            continue
        target = parse_qs(urlsplit(ref).query).get("file", [ref])[0]
        if target.lower().split("?")[0].endswith(".pdf"):
            full = urljoin(page_url, target)
            if full not in urls:
                urls.append(full)
    return urls


@dataclass(frozen=True)
class StoredPdf:
    source_url: str
    sha256: str
    n_pages: int
    storage_path: Path


def store_pdf(client: PoliteClient, pdf_url: str, pdf_dir: Path) -> StoredPdf:
    """Download a PDF and store it as <sha256>.pdf (identical files are stored once)."""
    tmp = pdf_dir / f"download-{hashlib.sha256(pdf_url.encode()).hexdigest()[:16]}.tmp"
    client.download(pdf_url, tmp)
    data = tmp.read_bytes()
    if not data.startswith(b"%PDF"):
        tmp.unlink()
        raise ValueError(f"{pdf_url} did not return a PDF")
    sha = hashlib.sha256(data).hexdigest()
    dest = pdf_dir / f"{sha}.pdf"
    if dest.exists():
        tmp.unlink()
    else:
        tmp.replace(dest)
    with pymupdf.open(dest) as doc:  # type: ignore[no-untyped-call]
        n_pages = doc.page_count
    return StoredPdf(source_url=pdf_url, sha256=sha, n_pages=n_pages, storage_path=dest)
