"""`seed`: insert a small demo dataset, but only into an empty `items` table.

Demo data lives here rather than in a migration: migrations are for schema, and data in a
migration would eventually run in production.
"""

import logging

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.db import advisory_lock, get_engine, wait_for_db
from app.models import Item

log = logging.getLogger(__name__)

SEED_LOCK_KEY = 1_000_002

_DEMO_WORDS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
    "Golf", "Hotel", "India", "Juliett", "Kilo", "Lima",
]  # fmt: skip


def demo_items(service_name: str) -> list[Item]:
    # Names carry the service name so a cross-service mix-up is obvious at a glance.
    return [
        Item(name=f"{service_name} {word}", description=f"Demo item seeded by {service_name}")
        for word in _DEMO_WORDS
    ]


def seed() -> int:
    """Seed the demo items if `items` is empty. Returns how many were inserted."""
    service_name = AppSettings().service_name
    engine = get_engine()
    try:
        wait_for_db(engine)
        # The lock makes check-then-insert safe if two tasks seed at the same time.
        with (
            engine.connect() as conn,
            advisory_lock(conn, SEED_LOCK_KEY),
            Session(bind=conn) as session,
        ):
            if session.scalar(select(exists().select_from(Item))):
                log.info("items table is not empty; skipping seed")
                return 0
            items = demo_items(service_name)
            session.add_all(items)
            session.commit()
        log.info("seeded %d demo items", len(items))
        return len(items)
    finally:
        engine.dispose()
