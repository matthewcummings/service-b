import pytest
from sqlalchemy import func, select, text

from app import cli
from app.db import get_engine
from app.errors import CommandFailed
from app.migrate import migrate
from app.models import Item
from app.seed import seed


def _scalar(db_settings, sql: str):
    engine = get_engine(db_settings)
    try:
        with engine.connect() as conn:
            return conn.scalar(text(sql))
    finally:
        engine.dispose()


def test_migrate_creates_schema_and_is_idempotent(db_settings):
    migrate()
    assert _scalar(db_settings, "SELECT version_num FROM alembic_version") == "0001"
    assert _scalar(db_settings, "SELECT to_regclass('public.items')::text") == "items"

    migrate()  # second run: nothing to do, no error
    assert _scalar(db_settings, "SELECT count(*) FROM alembic_version") == 1


STALE_BRANCH_MESSAGE = (
    "main's database is at migration f00dcafe1234, which this branch doesn't have. "
    "Merge or rebase main into your branch."
)


def _simulate_main_ahead_of_branch(db_settings) -> None:
    # As if main merged a migration after this branch was cut, and the preview DB was
    # copied from main.
    engine = get_engine(db_settings)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'f00dcafe1234'"))
    finally:
        engine.dispose()


def test_migrate_fails_clearly_on_unknown_revision(migrated_db):
    _simulate_main_ahead_of_branch(migrated_db)
    with pytest.raises(CommandFailed) as excinfo:
        migrate()
    assert str(excinfo.value) == STALE_BRANCH_MESSAGE


def test_cli_migrate_exits_nonzero_with_the_message(migrated_db, caplog):
    _simulate_main_ahead_of_branch(migrated_db)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["migrate"])
    assert excinfo.value.code == 1
    assert STALE_BRANCH_MESSAGE in caplog.text


def test_migrate_releases_advisory_lock(db_settings):
    migrate()
    assert _scalar(db_settings, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0


def test_seed_inserts_demo_items_once(migrated_db):
    inserted = seed()
    assert 10 <= inserted <= 20
    assert seed() == 0  # second run: table not empty, nothing inserted

    engine = get_engine(migrated_db)
    try:
        with engine.connect() as conn:
            names = conn.scalars(select(Item.name)).all()
    finally:
        engine.dispose()
    assert len(names) == inserted
    assert all(name.startswith("service-b ") for name in names)


def test_seed_skips_table_with_existing_data(migrated_db):
    engine = get_engine(migrated_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO items (name) VALUES ('added by a developer')"))
        assert seed() == 0
        with engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(Item)) == 1
    finally:
        engine.dispose()
