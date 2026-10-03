from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables (and a local .env file if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://drhp:drhp@localhost:5432/drhp"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
