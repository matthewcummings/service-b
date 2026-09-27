import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app import db as db_module
from app.config import AppSettings, DatabaseSettings, SourceDatabaseSettings
from app.db import get_engine

BASE = {"host": "db.example", "name": "service_b", "user": "service_b"}


@pytest.mark.parametrize("prefix", ["", "/a", "/team/a"])
def test_valid_path_prefixes(prefix):
    assert AppSettings(path_prefix=prefix).path_prefix == prefix


@pytest.mark.parametrize("prefix", ["a", "/a/", "/", "/a b"])
def test_invalid_path_prefixes_fail_loudly(prefix):
    with pytest.raises(ValidationError):
        AppSettings(path_prefix=prefix)


def test_password_auth_requires_password():
    with pytest.raises(ValidationError, match="needs a password"):
        DatabaseSettings(**BASE, auth="password")


def test_iam_auth_requires_region_and_ssl():
    with pytest.raises(ValidationError, match="AWS_REGION"):
        DatabaseSettings(**BASE, auth="iam", sslmode="require")
    with pytest.raises(ValidationError, match="sslmode"):
        DatabaseSettings(**BASE, auth="iam", sslmode="disable", aws_region="us-east-1")


def test_pool_size_defaults_and_env_overrides(monkeypatch):
    engine = get_engine(DatabaseSettings(**BASE, password="p"))
    assert (engine.pool.size(), engine.pool._max_overflow) == (5, 5)

    # Previews run smaller pools on the shared cluster.
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    engine = get_engine(DatabaseSettings(**BASE, password="p"))
    assert (engine.pool.size(), engine.pool._max_overflow) == (2, 3)


def test_source_settings_read_source_prefix(monkeypatch):
    monkeypatch.setenv("SOURCE_DB_HOST", "aurora.example")
    monkeypatch.setenv("SOURCE_DB_NAME", "service_b")
    monkeypatch.setenv("SOURCE_DB_USER", "service_b_reader")
    monkeypatch.setenv("SOURCE_DB_AUTH", "iam")
    monkeypatch.setenv("SOURCE_DB_SSLMODE", "require")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    source = SourceDatabaseSettings()
    assert (source.host, source.user, source.auth) == ("aurora.example", "service_b_reader", "iam")


class FakeRdsClient:
    """Stands in for boto3's RDS client; returns the container's password as the "token"."""

    def __init__(self, token: str):
        self.token = token
        self.calls: list[dict] = []

    def generate_db_auth_token(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.token


def test_iam_mode_generates_a_token_per_new_connection(postgres, monkeypatch):
    fake = FakeRdsClient(token=postgres.password)
    monkeypatch.setattr(db_module, "_rds_client", lambda region: fake)
    settings = DatabaseSettings(
        host=postgres.get_container_host_ip(),
        port=postgres.get_exposed_port(5432),
        name=postgres.dbname,
        user=postgres.username,
        auth="iam",
        sslmode="require",
        aws_region="us-east-1",
    )
    engine = get_engine(settings)
    try:
        with engine.connect() as conn:
            # IAM mode must connect over SSL, as Aurora requires.
            assert conn.scalar(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"))
        with engine.connect():
            pass  # reuses the pooled connection: no new token
        assert len(fake.calls) == 1

        with engine.connect(), engine.connect():
            pass  # two at once: the pool must open a second connection
        assert len(fake.calls) == 2
    finally:
        engine.dispose()

    assert fake.calls[0] == {
        "DBHostname": settings.host,
        "Port": settings.port,
        "DBUsername": settings.user,
        "Region": "us-east-1",
    }
