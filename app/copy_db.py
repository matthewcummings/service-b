"""`copy-db`: initialize a preview DB (`DB_*`) from main's DB (`SOURCE_DB_*`).

Preview DBs are logical databases on main's Aurora cluster, named `<main db>__<group>`
(e.g. `service_b__checkout`). On the env's first start the preview DB is empty: we copy
main's schema, rows and `alembic_version`, then `migrate` applies the branch's migrations on
top. On later starts (every push) the DB already has tables, and we keep them: preview data
survives pushes.

The copy streams `pg_dump | pg_restore`, so nothing is written to disk. `pg_dump` reads in a
single transaction, so the copy is a consistent snapshot even while main is being written.
"""

import logging
import os
import subprocess

from sqlalchemy import text

from app.config import DatabaseSettings, SourceDatabaseSettings
from app.db import get_engine, libpq_env, wait_for_db
from app.errors import CommandFailed

log = logging.getLogger(__name__)

# The preview DB roles default to a 30s statement_timeout, far too short for a restore.
# (pg_dump and pg_restore also set this themselves; being explicit costs nothing.)
NO_STATEMENT_TIMEOUT = {"PGOPTIONS": "-c statement_timeout=0"}


def check_target_is_preview_db(source: DatabaseSettings, target: DatabaseSettings) -> None:
    """Only ever write into a preview DB, never main's (or anything else)."""
    prefix = f"{source.name}__"
    if not (target.name.startswith(prefix) and len(target.name) > len(prefix)):
        raise CommandFailed(
            f"refusing to copy into {target.name!r}: copy-db only writes to preview databases, "
            f"whose names start with {prefix!r}"
        )


def _has_tables(db: DatabaseSettings) -> bool:
    engine = get_engine(db)
    try:
        wait_for_db(engine)
        with engine.connect() as conn:
            return bool(
                conn.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
                        " WHERE table_schema NOT IN ('pg_catalog', 'information_schema'))"
                    )
                )
            )
    finally:
        engine.dispose()


def copy_db(source: DatabaseSettings | None = None, target: DatabaseSettings | None = None) -> None:
    source = source or SourceDatabaseSettings()
    target = target or DatabaseSettings()

    check_target_is_preview_db(source, target)
    if _has_tables(target):
        log.info("target %s already initialized, keeping existing data", target.name)
        return

    log.info(
        "copying %s@%s/%s (%s auth) -> %s@%s/%s (%s auth)",
        source.user, source.host, source.name, source.auth,
        target.user, target.host, target.name, target.auth,
    )  # fmt: skip

    # --no-owner/--no-privileges: the preview DB has its own user, which simply owns
    # everything that is restored.
    dump = subprocess.Popen(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"],
        env=os.environ | libpq_env(source) | NO_STATEMENT_TIMEOUT,
        stdout=subprocess.PIPE,
    )
    assert dump.stdout is not None
    restore = subprocess.run(
        [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            # All or nothing: a half-restored DB would have tables, so the next start would
            # keep it as "already initialized".
            "--single-transaction",
            "--exit-on-error",
            f"--dbname={target.name}",
        ],
        env=os.environ | libpq_env(target) | NO_STATEMENT_TIMEOUT,
        stdin=dump.stdout,
    )
    # Close our copy of the pipe so pg_dump gets SIGPIPE if pg_restore exited early.
    dump.stdout.close()
    dump_rc = dump.wait()

    if dump_rc != 0 or restore.returncode != 0:
        raise CommandFailed(
            f"copy failed: pg_dump exit {dump_rc}, pg_restore exit {restore.returncode}"
        )
    log.info("copy complete")
