"""Alembic environment: runs migrations online against the service's own DB."""

from alembic import context

from app.db import get_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata  # enables `alembic revision --autogenerate`


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a database.")

if (connection := config.attributes.get("connection")) is not None:
    # `python -m app migrate` passes in a connection that already holds the advisory lock.
    run_migrations(connection)
else:
    # Plain `alembic ...` CLI use (e.g. `alembic revision --autogenerate` locally).
    engine = get_engine()
    try:
        with engine.connect() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()
