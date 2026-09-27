"""`migrate`: bring the schema up to the latest Alembic revision."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

from app.db import advisory_lock, get_engine, wait_for_db
from app.errors import CommandFailed

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Any fixed number works; Postgres scopes advisory locks to the current database, so
# each service's DB has its own independent lock.
MIGRATE_LOCK_KEY = 1_000_001


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    if connection is not None:
        # Picked up by migrations/env.py, so Alembic runs on our (locked) connection.
        config.attributes["connection"] = connection
    return config


def check_revision_is_known(conn: Connection, config: Config) -> None:
    """Fail clearly if the DB is at a migration this code doesn't have (a stale branch).

    A preview DB starts as a copy of main's. If main has since merged a migration, the
    branch can't be previewed faithfully: that combination would never reach production.
    """
    known = {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}
    for rev in MigrationContext.configure(conn).get_current_heads():
        if rev not in known:
            raise CommandFailed(
                f"this environment's database is at migration {rev}, which this code "
                "doesn't have. If main has moved on, merge or rebase main into your branch. "
                "If this preview's database got ahead of the branch (e.g. a rewritten "
                "migration), delete and re-push the branch to recreate it."
            )


def migrate() -> None:
    engine = get_engine()
    try:
        wait_for_db(engine)
        with engine.connect() as conn:
            # Preview DB roles default to a 30s statement_timeout; migrations may need longer.
            # A session-level SET survives the commits below.
            conn.execute(text("SET statement_timeout = 0"))
            with advisory_lock(conn, MIGRATE_LOCK_KEY):
                # Two tasks starting at once: the second waits here, then finds nothing to do.
                config = alembic_config(conn)
                check_revision_is_known(conn, config)
                # End the read transaction, so Alembic runs (and commits) its own.
                conn.commit()
                log.info("holding migration lock; running alembic upgrade head")
                command.upgrade(config, "head")
        log.info("migrations complete")
    finally:
        engine.dispose()
