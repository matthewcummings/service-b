"""Database access. This module is the only place that knows about the two auth modes.

- `password`: a plain password (local runs and tests).
- `iam`: a short-lived RDS IAM auth token instead of a password (Aurora, for main and
  previews). Tokens are signed locally by boto3 (no API call) and are only checked when a
  connection opens, so an open pooled connection keeps working after its 15 minutes run out.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

import boto3
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from app.config import DatabaseSettings

log = logging.getLogger(__name__)


@cache
def _rds_client(region: str):
    return boto3.client("rds", region_name=region)


def iam_auth_token(db: DatabaseSettings) -> str:
    assert db.aws_region  # guaranteed by DatabaseSettings validation in IAM mode
    return _rds_client(db.aws_region).generate_db_auth_token(
        DBHostname=db.host, Port=db.port, DBUsername=db.user, Region=db.aws_region
    )


def password_for(db: DatabaseSettings) -> str:
    """The password to log in with right now: the static one, or a fresh IAM token."""
    if db.auth == "iam":
        return iam_auth_token(db)
    assert db.password is not None  # guaranteed by DatabaseSettings validation
    return db.password.get_secret_value()


def libpq_env(db: DatabaseSettings) -> dict[str, str]:
    """Standard libpq variables, for command-line tools like pg_dump and pg_restore."""
    return {
        "PGHOST": db.host,
        "PGPORT": str(db.port),
        "PGDATABASE": db.name,
        "PGUSER": db.user,
        "PGPASSWORD": password_for(db),
        "PGSSLMODE": db.sslmode,
    }


def get_engine(db: DatabaseSettings | None = None) -> Engine:
    """Build a pooled engine for `db` (default: the service's own DB from `DB_*`)."""
    db = db or DatabaseSettings()
    url = URL.create(
        "postgresql+psycopg",
        username=db.user,
        password=db.password.get_secret_value() if db.auth == "password" and db.password else None,
        host=db.host,
        port=db.port,
        database=db.name,
    )
    engine = create_engine(
        url,
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        # Replaces connections that died while idle (DB restarts, failovers, network blips).
        pool_pre_ping=True,
        connect_args={"sslmode": db.sslmode},
    )

    if db.auth == "iam":
        # Runs only when the pool opens a new connection, so tokens are generated
        # once per connection, not once per query.
        @event.listens_for(engine, "do_connect")
        def _provide_iam_token(dialect, conn_rec, cargs, cparams):
            cparams["password"] = iam_auth_token(db)

    return engine


def wait_for_db(engine: Engine, timeout: float = 60.0, interval: float = 2.0) -> None:
    """Retry until the DB accepts connections, so one-shot commands survive brief blips."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            log.warning("database not ready yet (%s); retrying in %ss", exc.orig, interval)
            time.sleep(interval)


@contextmanager
def advisory_lock(conn: Connection, key: int) -> Iterator[None]:
    """Hold a session-level Postgres advisory lock for the duration of the block.

    Session-level (not transaction-level) so the lock survives the commits made inside the
    block. Other callers with the same key wait until it is released.
    """
    conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
    conn.commit()
    try:
        yield
    finally:
        # If the block failed mid-transaction, clear it so the unlock can run.
        conn.rollback()
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        conn.commit()
