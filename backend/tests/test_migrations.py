"""Schema versioning: fresh, legacy and migrating databases, all on scratch files."""

import os
import sqlite3

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import migrations
from app.migrations import MIGRATIONS, Migration, MigrationError, latest_version
from app.models import Document

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "schema_v1.sql")


def make_engine(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(conn, _r):
        conn.execute("PRAGMA foreign_keys=ON")

    return engine


def version(path) -> int:
    with sqlite3.connect(path) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def schema(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()


def legacy_db(tmp_path, rows=3):
    """A database as it was before versioning and before uploads: the committed schema-v1 fixture
    (rows in all four tables), user_version 0. rows=0 empties it, rows=4 adds a document."""
    path = str(tmp_path / "legacy.db")
    with open(FIXTURE, encoding="utf-8") as f:
        script = f.read()
    with sqlite3.connect(path) as c:
        c.executescript(script)
        if rows == 0:
            for table in ("extracted_values", "extraction_runs", "documents", "app_settings"):
                c.execute(f"DELETE FROM {table}")
        for i in range(3, rows):
            c.execute(
                "INSERT INTO documents (paperless_id, title, kind, ignored, created_at, updated_at)"
                " VALUES (?, 'synthetic', 'OTHER', 0, '2025-01-01', '2025-01-01')", (9000 + i,)
            )
        c.execute("PRAGMA user_version=0")
    return path


def add_column(conn):
    conn.execute("ALTER TABLE documents ADD COLUMN note TEXT DEFAULT ''")


# the next migration after the last real one: what "a later release" adds on top
NEXT = latest_version() + 1
V3 = Migration(NEXT, "add note", add_column)


def test_fresh_db_is_created_and_stamped_latest(tmp_path):
    path = str(tmp_path / "fresh.db")
    engine = make_engine(path)
    result = migrations.run(engine)
    assert result["fresh"] and result["backup"] is None
    assert version(path) == latest_version()
    names = {row[1] for row in schema(path) if row[0] == "table"}
    assert {"documents", "app_settings", "extracted_values", "extraction_runs"} <= names
    assert not os.path.exists(tmp_path / "backups")


def test_legacy_db_is_baselined_and_upgraded(tmp_path):
    path = legacy_db(tmp_path)
    engine = make_engine(path)
    result = migrations.run(engine)
    assert result["from"] == 0 and result["applied"] == [1, 2, 3, 4] and not result["fresh"]
    assert version(path) == latest_version()
    with Session(engine) as s:
        assert len(s.exec(Document.__table__.select()).all()) == 3
    assert result["backup"]  # migration 2 changes a database that holds data


def test_run_twice_changes_nothing(tmp_path):
    path = legacy_db(tmp_path)
    engine = make_engine(path)
    migrations.run(engine, [*MIGRATIONS, V3])
    first = schema(path)
    result = migrations.run(engine, [*MIGRATIONS, V3])
    assert result["applied"] == [] and result["backup"] is None
    assert schema(path) == first
    assert version(path) == NEXT


def test_backup_before_a_data_changing_migration_is_openable(tmp_path):
    path = legacy_db(tmp_path, rows=4)
    result = migrations.run(make_engine(path), [*MIGRATIONS, V3])
    assert result["applied"] == [1, 2, 3, 4, NEXT]
    backup = result["backup"]
    assert backup and os.path.dirname(backup) == str(tmp_path / "backups")
    assert os.path.basename(backup).startswith("pre-v2-")  # named after the first migration that ran
    with sqlite3.connect(backup) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 4
        cols = [r[1] for r in c.execute("PRAGMA table_info(documents)")]
        assert "note" not in cols and "source" not in cols  # it is the state before the migrations
    assert version(backup) == 0


def test_empty_database_gets_no_backup(tmp_path):
    path = legacy_db(tmp_path, rows=0)
    assert migrations.run(make_engine(path), [*MIGRATIONS, V3])["backup"] is None


def test_failed_migration_rolls_back_completely(tmp_path):
    path = legacy_db(tmp_path)

    def broken(conn):
        conn.execute("ALTER TABLE documents ADD COLUMN half_done TEXT")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        migrations.run(make_engine(path), [*MIGRATIONS, Migration(NEXT, "broken", broken)])
    assert version(path) == NEXT - 1  # the real migrations landed, the broken one did not
    with sqlite3.connect(path) as c:
        assert "half_done" not in [r[1] for r in c.execute("PRAGMA table_info(documents)")]


def test_table_rebuild_runs_with_foreign_keys_off(tmp_path):
    path = legacy_db(tmp_path)
    seen = {}

    def rebuild(conn):
        # new table, copy, drop the old one, rename: renaming the OLD table first would drag the
        # foreign keys of extracted_values and extraction_runs along with it
        seen["fk"] = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("CREATE TABLE documents_new (id INTEGER PRIMARY KEY, paperless_id INTEGER, title TEXT)")
        conn.execute("INSERT INTO documents_new SELECT id, paperless_id, title FROM documents")
        conn.execute("DROP TABLE documents")
        conn.execute("ALTER TABLE documents_new RENAME TO documents")

    m = Migration(NEXT, "rebuild", rebuild, foreign_keys_off=True)
    migrations.run(make_engine(path), [*MIGRATIONS, m])
    assert seen["fk"] == 0
    assert version(path) == NEXT
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 3


def test_rebuild_that_orphans_rows_is_refused(tmp_path):
    path = legacy_db(tmp_path)
    def orphan(conn):
        conn.execute("DELETE FROM documents")

    with pytest.raises(MigrationError):
        migrations.run(make_engine(path), [*MIGRATIONS, Migration(NEXT, "orphan", orphan, foreign_keys_off=True)])
    assert version(path) == NEXT - 1
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 3


def test_newer_database_is_refused(tmp_path):
    path = legacy_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version=99")
    with pytest.raises(MigrationError):
        migrations.run(make_engine(path))


def test_missing_tables_are_added_to_a_legacy_db(tmp_path):
    path = str(tmp_path / "old.db")
    with open(FIXTURE, encoding="utf-8") as f:
        script = f.read()
    with sqlite3.connect(path) as c:
        c.executescript(script[: script.index("CREATE TABLE app_settings")])  # documents only
    migrations.run(make_engine(path))
    names = {row[1] for row in schema(path) if row[0] == "table"}
    assert "app_settings" in names
