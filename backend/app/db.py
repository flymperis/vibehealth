"""Database engine and schema creation."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, create_engine

from . import hardening
from .config import get_settings

_settings = get_settings()
hardening.apply_umask()  # before the first file (the data folder, the database) is made
os.makedirs(_settings.data_dir, mode=0o700, exist_ok=True)

engine = create_engine(
    f"sqlite:///{_settings.database_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    # WAL keeps the background sync from blocking page loads. It also means the
    # data lives partly in vibehealth.db-wal: a backup has to take all three
    # files, or use VACUUM INTO.
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def init_db() -> dict:
    """Create or migrate the schema (see migrations.py). Runs before anything else."""
    from . import migrations

    return migrations.run(engine)
