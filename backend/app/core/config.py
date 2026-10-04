from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> repository root is three levels above app/.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """App settings, read from environment variables (and a local .env file if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local runs talk to the docker compose Postgres published on host port 5433.
    database_url: str = "postgresql+psycopg://drhp:drhp@localhost:5433/drhp"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: str = ""

    # Raw PDFs, caches and ingest inputs (gitignored). Containers set DATA_DIR=/app/data.
    data_dir: Path = _REPO_ROOT / "data"

    # Polite scraping (build guide Phase 1 step 6): 1 request every 2-3 s, custom User-Agent.
    http_user_agent: str = (
        "drhp-redflag-research/0.1 (+https://github.com/shauryagupta575/drhp-redflag)"
    )
    http_min_interval_s: float = 2.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
