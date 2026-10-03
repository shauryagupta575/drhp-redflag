# DRHP Red-Flag Agent + IPO Backtest

An AI agent that reads Indian IPO prospectuses like an analyst, cites every warning, and proves its value against real post-listing returns.

> **Status:** Phase 0 (setup and foundations). Skeleton only: API, worker, Postgres (pgvector), Redis and a frontend that checks API health.

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

## Development

```bash
cd backend
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy app tests
uv run pytest
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
