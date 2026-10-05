# DRHP Red-Flag Agent + IPO Backtest

An AI agent that reads Indian IPO prospectuses like an analyst, cites every warning, and proves its value against real post-listing returns.

> **Status:** Phase 2 (PDF parsing and section location). Offer documents are parsed into page-numbered text, tables and pgvector embeddings, and their key sections are located.

## Quick start

Requirements: Docker, Node.js 20.19+ (or 22.12+), [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # then add your GEMINI_API_KEY
docker compose up --build     # api, worker, postgres, redis
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173 → shows "API healthy"
```

The API is on http://localhost:8000 (`GET /health`, docs at `/docs`). The Vite dev server proxies `/health` to it.

## Data collection (Phase 1)

### Inputs (manual, gitignored under `data/raw/`)

Automated scraping of the listing pages is not possible within the sites' rules, so three
small index files are obtained by hand once:

| File | Source | How |
|---|---|---|
| `data/raw/nse/IPO-PastIssue-*.csv` | NSE, [Past Issues](https://www.nseindia.com/market-data/all-upcoming-issues-ipo) | Past Issues tab → Custom → 01-01-2019 to 31-12-2025 → **Download (.csv)**. NSE's terms of use prohibit automated data collection; the download button is the sanctioned route. |
| `data/raw/sebi/rhp_index.txt` | SEBI, Filings → Public Issues → [Red Herring Documents filed with ROC](https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=11) | One line per listing row: `YYYY-MM-DD\|<month-folder>/<slug>_<id>.html` (the filing page path after `/filings/public-issues/`), filings dated 2018-11-01 to 2025-12-31. SEBI's firewall blocks scripted paging of this listing, so it was read in a browser. |
| `data/raw/sebi/drhp_index.txt` | SEBI, [Draft Offer Documents filed with SEBI](https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=10) | Same format, filings dated 2017-01-01 to 2025-12-31. |

Individual SEBI filing pages and PDFs are fetched by the script (allowed by SEBI's robots.txt;
SEBI's website policy permits linking to its documents). yfinance supplies prices.

### Run

```bash
docker compose up -d postgres          # Postgres on localhost:5433
cd backend
uv run alembic upgrade head
uv run python -m app.ingest.run        # universe → documents → prices → report
uv run python -m app.ingest.run --steps documents --limit 5   # partial runs
```

The script is idempotent: PDFs are stored once as `data/pdfs/<sha256>.pdf`, every fetched page
is cached in `data/cache/`, and re-runs skip documents and price series already stored.
Scraping is polite: one request every 2.5 s per host, a custom User-Agent, robots.txt checked,
and it stops at the first block instead of retrying.

- **Universe:** NSE security types `EQ`/`BE` (mainboard). SME, debt, InvIT/REIT rows,
  follow-on offers, partly-paid issues and withdrawn issues are excluded; the report lists every
  excluded row and why.
- **Documents:** each IPO is matched to SEBI filings by company name within a filing-date
  window; the RHP is preferred and the DRHP kept too.
- **Prices:** daily closes (split-adjusted, not dividend-adjusted) from listing date to
  +12 months for `<SYMBOL>.NS`, plus the Nifty 50 benchmark `^NSEI`.
- **Corrections:** gaps can be filled by hand in
  [`backend/app/ingest/seed/overrides.csv`](backend/app/ingest/seed/overrides.csv)
  (listing dates, Yahoo tickers, exclusions, SEBI filing paths; see `app/ingest/overrides.py`).

Outputs: tables `companies`, `ipos`, `documents`, `prices`, plus
`data/processed/universe.csv` and `data/processed/ingest_report.md`.

## PDF parsing and section location (Phase 2)

```bash
cd backend
uv run python -m app.parsing.run                   # every downloaded document
uv run python -m app.parsing.run --doc-type DRHP   # or --document-ids 3,7 [--force]
```

For each document:

1. **Pages** → `pages`: PyMuPDF text per 1-based PDF page, with text-block coordinates
   (`blocks`) for later citation highlighting. Pages without a text layer go to the
   **Tesseract** OCR fallback (`is_scanned = true`).
2. **Chunks** → `chunks`: ~500-token windows with ~100-token overlap, embedded locally with
   `BAAI/bge-small-en-v1.5` (384 dimensions, no API cost) and stored in pgvector. The guide
   suggests ~800 tokens; bge-small reads at most 512, so chunks are smaller so every token is
   embedded.
3. **Section map** → `documents.section_map`, e.g. `{"related_party": [312, 318]}` (PDF pages):
   the document's own table of contents, mapped from printed to PDF page numbers via footer
   page numbers, confirmed by the heading on the start page and the start of the next section,
   plus an embedding rank (pgvector). Related-party transactions, usually a note inside the
   restated financials, are found by heading search when the TOC has no chapter for them or
   only a cross-reference.
4. **Tables** → `pages.tables`: pdfplumber tables (JSON) from the restated-financials pages.

Sections located: restated financials, related party, outstanding litigation, capital
structure, objects of the issue, risk factors, promoters, group companies, basis for issue
price, financial indebtedness.

`data/processed/section_check.md` is the spot-check sheet: per document and section, the page
range, how it was found, whether the start heading and the following section were confirmed,
the embedding rank, and the first lines of the start page and of the page after the end.
Tesseract is needed locally for OCR (`brew install tesseract`); the Docker image includes it.

## Development

```bash
cd backend
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run pytest                # integration tests use database drhp_test on localhost:5433
uv run pre-commit install    # run from backend/; installs hooks for the whole repo
```

## Repository layout

```
backend/    FastAPI app, Celery workers, Alembic migrations (app/db/migrations)
frontend/   React 18 + TypeScript + Vite + Tailwind + shadcn/ui
eval/       labels/, reports/ (evaluation set and harness)
notebooks/  backtest and EDA notebooks
data/       raw PDFs and caches (gitignored)
infra/      production compose, Caddyfile, backups
docs/       architecture, methodology, API docs
```

## License

MIT. Educational research, not investment advice.
