"""Shared fixtures: a throwaway Postgres 17 in Docker (testcontainers), no compose needed.

Tests configure the app exactly like production does: through DB_* environment variables.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from app.api import create_app
from app.config import AppSettings, DatabaseSettings
from app.db import get_engine
from app.migrate import migrate
from tests.pg import SSL_ARGS, db_env, postgres_container


def reset_schema(db: DatabaseSettings) -> None:
    engine = get_engine(db)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with postgres_container().with_command(SSL_ARGS) as pg:
        yield pg


@pytest.fixture
def db_settings(postgres, monkeypatch) -> DatabaseSettings:
    """Point DB_* at the session container, with an empty schema."""
    for key, value in db_env(postgres).items():
        monkeypatch.setenv(key, value)
    settings = DatabaseSettings()
    reset_schema(settings)
    return settings


@pytest.fixture
def migrated_db(db_settings) -> DatabaseSettings:
    migrate()
    return db_settings


@pytest.fixture
def client(migrated_db) -> Iterator[TestClient]:
    app = create_app(AppSettings(env_name="test", git_sha="abc123", git_branch="main"))
    with TestClient(app) as test_client:  # `with` runs the lifespan (engine setup/teardown)
        yield test_client
