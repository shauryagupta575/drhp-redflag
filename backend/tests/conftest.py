import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# Integration tests use their own database next to the dev one (compose Postgres, host
# port 5433). CI provides a Postgres service and sets TEST_DATABASE_URL.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://drhp:drhp@localhost:5433/drhp_test"
)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


def _alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "app/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    try:
        _ensure_database(TEST_DATABASE_URL)
    except OperationalError as exc:
        if os.environ.get("REQUIRE_TEST_DB"):
            raise
        pytest.skip(f"Postgres not reachable for integration tests: {exc.orig}")
    cfg = _alembic_config(TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, ipos, companies, prices RESTART IDENTITY CASCADE"))
    with Session(db_engine) as session:
        yield session
