"""copy-db: initialize a preview DB (`service_b__<group>`) from main's DB (`service_b`).

Like Aurora in AWS, one Postgres server holds both: main's DB and the preview DB.

The copies run `copy-db` inside the real Docker image, because pg_dump must be version 17
to dump a Postgres 17 server and the image is what guarantees that (the host may have any
version, or none). The name guard runs before any pg_dump, so it is tested in-process.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest
from sqlalchemy import text

from app.config import DatabaseSettings
from app.copy_db import check_target_is_preview_db
from app.db import get_engine
from app.errors import CommandFailed
from app.migrate import migrate
from app.seed import seed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_TAG = "service-b:pytest"
PREVIEW_DB = "service_b__cart"


@pytest.fixture(scope="module")
def image() -> str:
    # Layer caching makes this fast after the first run.
    subprocess.run(["docker", "build", "--quiet", "-t", IMAGE_TAG, str(PROJECT_ROOT)], check=True)
    return IMAGE_TAG


@pytest.fixture
def main_db(migrated_db) -> DatabaseSettings:
    """main's DB: migrated, seeded, plus a row a developer added later."""
    seed()
    rows(migrated_db, "INSERT INTO items (name) VALUES ('added on main after seeding')")
    return migrated_db


@pytest.fixture
def preview_db(main_db) -> Iterator[DatabaseSettings]:
    """An empty preview DB on the same server, as the env stack's custom resource creates."""
    admin = get_engine(main_db).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {PREVIEW_DB}"))
    yield main_db.model_copy(update={"name": PREVIEW_DB})
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {PREVIEW_DB} WITH (FORCE)"))
    admin.dispose()


def rows(db: DatabaseSettings, sql: str) -> list[tuple]:
    """Run one statement (committed) and return its rows."""
    engine = get_engine(db)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def run_copy_db(image: str, postgres) -> str:
    """Run `copy-db` in the app image, sharing the Postgres container's network (localhost)."""
    common = {"HOST": "localhost", "USER": postgres.username, "AUTH": "password"}
    common |= {"PASSWORD": postgres.password, "SSLMODE": "disable"}
    env = {f"DB_{key}": value for key, value in common.items()} | {"DB_NAME": PREVIEW_DB}
    env |= {f"SOURCE_DB_{key}": value for key, value in common.items()}
    env |= {"SOURCE_DB_NAME": postgres.dbname}
    output = docker.from_env().containers.run(
        image,
        ["copy-db"],
        environment=env,
        network_mode=f"container:{postgres.get_wrapped_container().id}",
        remove=True,
        stderr=True,
    )
    return output.decode()


ITEMS_SQL = "SELECT id, name, description, created_at FROM items ORDER BY id"


def test_copy_into_empty_preview_db_then_migrate(image, postgres, main_db, preview_db, monkeypatch):
    main_items = rows(main_db, ITEMS_SQL)

    run_copy_db(image, postgres)

    assert rows(preview_db, ITEMS_SQL) == main_items
    assert rows(preview_db, "SELECT version_num FROM alembic_version") == [("0001",)]

    # The branch's migrations run on top of the copy (here: already at head, so a no-op).
    monkeypatch.setenv("DB_NAME", PREVIEW_DB)
    migrate()
    assert rows(preview_db, "SELECT version_num FROM alembic_version") == [("0001",)]

    # The identity sequence came along too, so new rows don't collide with copied ids.
    [(new_id,)] = rows(preview_db, "INSERT INTO items (name) VALUES ('preview') RETURNING id")
    assert new_id > max(item[0] for item in main_items)


def test_second_run_keeps_preview_data(image, postgres, main_db, preview_db):
    run_copy_db(image, postgres)
    rows(preview_db, "INSERT INTO items (name) VALUES ('entered in the preview')")
    rows(main_db, "INSERT INTO items (name) VALUES ('added on main later')")
    preview_items = rows(preview_db, ITEMS_SQL)

    output = run_copy_db(image, postgres)  # e.g. the next push restarts the task

    assert "already initialized, keeping existing data" in output
    assert rows(preview_db, ITEMS_SQL) == preview_items


@pytest.mark.parametrize(
    "target_name",
    ["service_b", "service_b__", "service_b_reader", "service_a__cart", "other__service_b__x"],
)
def test_refuses_target_that_is_not_a_preview_db(target_name):
    source = DatabaseSettings(host="h", name="service_b", user="u", password="p")
    target = source.model_copy(update={"name": target_name})
    with pytest.raises(CommandFailed, match="only writes to preview databases"):
        check_target_is_preview_db(source, target)


def test_accepts_preview_db_name():
    source = DatabaseSettings(host="h", name="service_b", user="u", password="p")
    check_target_is_preview_db(source, source.model_copy(update={"name": "service_b__cart"}))
