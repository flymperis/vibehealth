"""Migration 2 (documents can be uploaded) against the committed schema-v1 fixture.

The fixture is a database as the previous release left it, with rows in all four tables. The
migration rebuilds `documents`: these tests check that nothing is lost on the way (ids, every
extracted value), that the result is exactly what a fresh install gets, and that a failure
leaves the original untouched.
"""

import os
import sqlite3

import pytest
from sqlmodel import SQLModel

from app import migrations
from app.migrations import MIGRATIONS, Migration, MigrationError
from tests.test_migrations import legacy_db, make_engine, version

TABLES = ("documents", "extracted_values", "extraction_runs", "app_settings")


def dump(path: str) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as c:
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}


def shape(path: str) -> dict:
    """Everything about the structure that the app depends on, per table."""
    out = {}
    with sqlite3.connect(path) as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in names:
            indexes = {}
            for _seq, name, unique, origin, partial in c.execute(f'PRAGMA index_list("{t}")'):
                cols = [(r[2], r[5]) for r in c.execute(f'PRAGMA index_xinfo("{name}")') if r[5]]
                indexes[name] = (unique, origin, partial, cols)
            out[t] = {
                "columns": [tuple(r[1:]) for r in c.execute(f'PRAGMA table_info("{t}")')],  # name..pk
                "indexes": indexes,
                "fks": sorted(
                    (r[2], r[3], r[4], r[5], r[6]) for r in c.execute(f'PRAGMA foreign_key_list("{t}")')
                ),
            }
        out["__index_sql__"] = sorted(
            (r[0], " ".join((r[1] or "").split()))
            for r in c.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        )
    return out


def fresh_shape(tmp_path) -> dict:
    path = str(tmp_path / "fresh.db")
    engine = make_engine(path)
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return shape(path)


def test_rows_and_ids_survive(tmp_path):
    path = legacy_db(tmp_path)
    before = dump(path)
    assert [len(before[t]) for t in TABLES] == [3, 6, 2, 2]
    assert [r[0] for r in before["documents"]] == [1, 5, 9]

    result = migrations.run(make_engine(path))
    assert result["applied"] == [1, 2] and version(path) == 2

    after = dump(path)
    assert {t: len(rows) for t, rows in after.items()} == {t: len(rows) for t, rows in before.items()}
    # every old column of every document is unchanged, in place, with the same id ...
    assert [r[:8] for r in after["documents"]] == before["documents"]
    # ... and it is a Paperless document
    assert {r[8] for r in after["documents"]} == {"paperless"}
    assert {r[9:] for r in after["documents"]} == {(None,) * 5}
    # the other three tables are exactly what they were: no cascade ran
    for table in ("extracted_values", "extraction_runs", "app_settings"):
        assert after[table] == before[table]
    with sqlite3.connect(path) as c:
        # every value still resolves to its document
        joined = c.execute(
            "SELECT count(*) FROM extracted_values v JOIN documents d ON d.id = v.document_id"
        ).fetchone()[0]
        assert joined == 6
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_result_equals_a_fresh_install(tmp_path):
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path))
    assert shape(path) == fresh_shape(tmp_path)


def test_fresh_install_is_created_at_the_latest_version(tmp_path):
    path = str(tmp_path / "new.db")
    result = migrations.run(make_engine(path))
    assert result["fresh"] and version(path) == migrations.latest_version() == 2
    assert result["backup"] is None
    assert shape(path) == fresh_shape(tmp_path)


def test_running_again_is_a_no_op(tmp_path):
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path))
    before, structure = dump(path), shape(path)
    backups = sorted(os.listdir(tmp_path / "backups"))
    result = migrations.run(make_engine(path))
    assert result["applied"] == [] and result["backup"] is None
    assert dump(path) == before and shape(path) == structure
    assert sorted(os.listdir(tmp_path / "backups")) == backups
    assert version(path) == 2


def test_a_backup_of_the_old_database_is_taken_first(tmp_path):
    path = legacy_db(tmp_path)
    before = dump(path)
    backup = migrations.run(make_engine(path))["backup"]
    assert os.path.basename(backup).startswith("pre-v2-")
    assert dump(backup) == before  # exactly the pre-migration state, old schema included
    with sqlite3.connect(backup) as c:
        assert "source" not in [r[1] for r in c.execute("PRAGMA table_info(documents)")]
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failure_in_the_middle_rolls_back_and_names_the_backup(tmp_path):
    path = legacy_db(tmp_path)
    before, structure = dump(path), shape(path)

    def rebuild_then_fail(conn):
        migrations._uploads(conn)  # the whole rebuild happens ...
        assert "source" in [r[1] for r in conn.execute("PRAGMA table_info(documents)")]
        raise RuntimeError("synthetic failure after the rebuild")

    broken = Migration(2, "broken uploads", rebuild_then_fail, foreign_keys_off=True)
    with pytest.raises(MigrationError) as info:
        migrations.run(make_engine(path), [MIGRATIONS[0], broken])

    message = str(info.value)
    backups = os.listdir(tmp_path / "backups")
    assert len(backups) == 1 and backups[0] in message  # names the backup file
    assert "Refusing to start" in message and "rolled back" in message
    # ... and none of it stuck: same rows, same structure, still version 1
    assert version(path) == 1
    assert dump(path) == before and shape(path) == structure
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "documents_new" not in [r[0] for r in c.execute("SELECT name FROM sqlite_master")]
    # the same start works once the cause is gone
    assert migrations.run(make_engine(path))["applied"] == [2]
    assert {t: len(r) for t, r in dump(path).items()} == {t: len(r) for t, r in before.items()}


def test_dropping_documents_with_foreign_keys_on_would_cascade_and_the_runner_keeps_them_off(tmp_path):
    """The hazard the runner guards against, shown and then shown to be guarded.

    Control: on a plain connection with foreign keys ON, dropping `documents` deletes every extracted
    value and run through ON DELETE CASCADE. Through the runner the same DROP happens with foreign keys
    OFF (and `PRAGMA foreign_keys=ON` inside the transaction is inert): the children are still there
    while the migration runs; the migration then fails only because its rows are orphaned, and all of
    it is rolled back."""
    path = legacy_db(tmp_path)
    control = str(tmp_path / "control.db")
    with open(path, "rb") as src, open(control, "wb") as dst:
        dst.write(src.read())
    with sqlite3.connect(control, isolation_level=None) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("DROP TABLE documents")
        assert c.execute("SELECT count(*) FROM extracted_values").fetchone()[0] == 0  # the cascade
        assert c.execute("SELECT count(*) FROM extraction_runs").fetchone()[0] == 0

    seen = {}

    def drop_documents(conn):
        conn.execute("PRAGMA foreign_keys=ON")  # a no-op inside a transaction
        seen["fk"] = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("DROP TABLE documents")
        seen["values"] = conn.execute("SELECT count(*) FROM extracted_values").fetchone()[0]
        seen["runs"] = conn.execute("SELECT count(*) FROM extraction_runs").fetchone()[0]

    with pytest.raises(MigrationError):
        migrations.run(make_engine(path), [MIGRATIONS[0], Migration(2, "drop", drop_documents, foreign_keys_off=True)])
    assert seen == {"fk": 0, "values": 6, "runs": 2}  # no cascade ran during the migration
    with sqlite3.connect(path) as c:  # and the failed migration was rolled back as a whole
        assert c.execute("SELECT count(*) FROM extracted_values").fetchone()[0] == 6
        assert c.execute("SELECT count(*) FROM extraction_runs").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 3


def test_the_runner_refuses_to_go_on_when_foreign_keys_cannot_be_switched_off(tmp_path, monkeypatch):
    path = legacy_db(tmp_path)
    before = dump(path)
    real_connect = sqlite3.connect

    class Stuck:
        """A connection whose `PRAGMA foreign_keys` always answers ON, as if it could not be changed."""

        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("PRAGMA FOREIGN_KEYS"):
                return self._c.execute("SELECT 1")  # set or asked: always "on"
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(migrations.sqlite3, "connect", lambda *a, **kw: Stuck(real_connect(*a, **kw)))
    with pytest.raises(MigrationError, match="foreign_keys"):
        migrations.run(make_engine(path))
    monkeypatch.undo()
    assert dump(path) == before and version(path) == 1  # only the baseline stamp (no data change) had landed


def test_a_rebuild_that_loses_a_document_is_refused(tmp_path):
    path = legacy_db(tmp_path)

    def lossy(conn):
        migrations._uploads(conn)
        conn.execute("DELETE FROM documents WHERE id = 5")  # extracted_values still point at it

    with pytest.raises(MigrationError):
        migrations.run(make_engine(path), [MIGRATIONS[0], Migration(2, "lossy", lossy, foreign_keys_off=True)])
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM extracted_values").fetchone()[0] == 6


def test_a_database_already_in_the_new_shape_is_left_alone(tmp_path):
    """user_version 0 with the new columns present (e.g. built by create_all): nothing to rebuild."""
    path = str(tmp_path / "odd.db")
    engine = make_engine(path)
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version=0")
        c.execute(
            "INSERT INTO documents (paperless_id, title, kind, ignored, created_at, updated_at)"
            " VALUES (1, 't', 'OTHER', 0, '2026-01-01', '2026-01-01')"
        )
    structure = shape(path)
    assert migrations.run(make_engine(path))["applied"] == [1, 2]
    assert shape(path) == structure and version(path) == 2
    assert dump(path)["documents"][0][8] == "paperless"


def test_the_previous_releases_sql_still_works_on_the_migrated_schema(tmp_path):
    """What this proves, and no more: `source` has a DEFAULT and the previous release's statements never
    name it, so those STATEMENTS run unchanged on the migrated tables. It does not prove that the
    previous release starts on a v2 database: it does not (its version guard refuses a database newer
    than it knows, see test_migrations.test_newer_database_is_refused). To go back, restore
    the pre-migration backup."""
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path))
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute(
            "INSERT INTO documents (paperless_id, title, kind, doc_date, ignored, created_at, updated_at)"
            " VALUES (4242, 'inserted by the previous release', 'REPORT', NULL, 0, '2026-01-01', '2026-01-01')"
        )
        row = c.execute(
            "SELECT id, paperless_id, title, kind, doc_date, ignored, created_at, updated_at, source"
            " FROM documents WHERE paperless_id = 4242"
        ).fetchone()
        assert row[-1] == "paperless"
        with pytest.raises(sqlite3.IntegrityError):  # its unique paperless_id still holds
            c.execute(
                "INSERT INTO documents (paperless_id, title, kind, ignored, created_at, updated_at)"
                " VALUES (4242, 'again', 'REPORT', 0, '2026-01-01', '2026-01-01')"
            )


def test_new_indexes_behave(tmp_path):
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path))
    insert = (
        "INSERT INTO documents (paperless_id, title, kind, ignored, created_at, updated_at, source, sha256)"
        " VALUES (?, 't', 'OTHER', 0, '2026-01-01', '2026-01-01', ?, ?)"
    )
    with sqlite3.connect(path) as c:
        c.execute(insert, (None, "upload", "a" * 64))
        c.execute(insert, (None, "upload", "b" * 64))  # several NULL paperless_ids
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(insert, (None, "upload", "a" * 64))  # the same upload twice
        c.execute(insert, (7001, "paperless", "a" * 64))  # the hash index only covers uploads
        c.execute(insert, (7002, "paperless", "a" * 64))
