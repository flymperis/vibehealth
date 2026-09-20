"""Migration 2 hardening: what the rebuild refuses to do, how the backup is checked, crash-restart
loops, and two starts at once. All on scratch databases built from the committed schema-v1 fixture."""

import os
import sqlite3

import pytest

from app import migrations
from app.migrations import MIGRATIONS, Migration, MigrationError
from tests.test_migration_uploads import dump, fresh_shape, shape
from tests.test_migrations import V3, legacy_db, make_engine, version


def backups_of(tmp_path) -> list[str]:
    folder = tmp_path / "backups"
    return sorted(os.listdir(folder)) if folder.exists() else []


UNEXPECTED = {
    "an extra column": "ALTER TABLE documents ADD COLUMN note TEXT DEFAULT ''",
    "an extra index": "CREATE INDEX ix_documents_title ON documents (title)",
    "an extra unique index": "CREATE UNIQUE INDEX ux_documents_title ON documents (title)",
    "an index that is not the v1 one": (
        "DROP INDEX ix_documents_ignored; CREATE UNIQUE INDEX ix_documents_ignored ON documents (ignored, kind)"),
    "a trigger on documents": "CREATE TRIGGER trg_docs AFTER UPDATE ON documents BEGIN SELECT 1; END",
    "a trigger elsewhere that names documents": (
        "CREATE TRIGGER trg_other AFTER INSERT ON app_settings BEGIN DELETE FROM documents WHERE id = -1; END"),
    "a view over documents": "CREATE VIEW v_docs AS SELECT id, title FROM documents",
    "a leftover documents_new": "CREATE TABLE documents_new (id INTEGER PRIMARY KEY)",
}


@pytest.mark.parametrize("what", list(UNEXPECTED))
def test_an_unexpected_object_is_refused_with_the_data_and_the_object_intact(tmp_path, what):
    path = legacy_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.executescript(UNEXPECTED[what])
    before = {t: rows for t, rows in dump(path).items()}
    with sqlite3.connect(path) as c:
        objects = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()

    with pytest.raises(MigrationError) as info:
        migrations.run(make_engine(path))

    message = str(info.value)
    backups = backups_of(tmp_path)
    assert len(backups) == 1 and backups[0] in message  # the message names the backup
    assert "not exactly what schema version 1 had" in message and "Refusing to start" in message
    assert "Nothing was dropped or changed" in message
    assert version(path) == 1  # the baseline stamp only: migration 2 did not land
    assert dump(path) == before
    with sqlite3.connect(path) as c:  # nothing was dropped or renamed, not even the odd thing
        assert c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == objects
        assert "source" not in [r[1] for r in c.execute("PRAGMA table_info(documents)")]
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert dump(str(tmp_path / "backups" / backups[0])) == before


def test_the_untouched_v1_shape_is_accepted(tmp_path):
    path = legacy_db(tmp_path)
    with sqlite3.connect(path) as c:
        assert migrations._v1_shape_problems(c) == []
    assert migrations.run(make_engine(path))["applied"] == [1, 2, 3, 4]
    assert shape(path) == fresh_shape(tmp_path)


def test_a_refused_start_can_be_repeated_without_harm(tmp_path):
    path = legacy_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute("CREATE INDEX ix_documents_title ON documents (title)")
    before = dump(path)
    for _ in range(3):
        with pytest.raises(MigrationError):
            migrations.run(make_engine(path))
        assert dump(path) == before
    assert len(backups_of(tmp_path)) == 3  # one per attempt, never more


# --- the backup is checked ---------------------------------------------------------------------------------


def corrupting(monkeypatch, how):
    """Damage each new backup between the copy and its check (what a bad disk or a crash would do)."""
    real = migrations.verify_backup

    def damaged(path, expected=None):
        if how == "truncated":
            with open(path, "r+b") as f:
                f.truncate(50)
        elif how == "garbage page":
            with open(path, "r+b") as f:
                f.seek(4096 * 2)
                f.write(b"\xff" * 4096)
        elif how == "missing rows":
            c = sqlite3.connect(path)  # (closed for real: an open handle would keep the file on Windows)
            try:
                c.execute("PRAGMA foreign_keys=OFF")
                c.execute("DELETE FROM extracted_values WHERE id = (SELECT min(id) FROM extracted_values)")
                c.commit()
            finally:
                c.close()
        return real(path, expected)

    monkeypatch.setattr(migrations, "verify_backup", damaged)


@pytest.mark.parametrize("how", ["truncated", "garbage page", "missing rows"])
def test_a_bad_backup_is_detected_and_removed_and_the_start_is_refused(tmp_path, monkeypatch, how):
    path = legacy_db(tmp_path)
    before = dump(path)
    corrupting(monkeypatch, how)
    with pytest.raises(MigrationError, match="Could not make the backup"):
        migrations.run(make_engine(path))
    assert backups_of(tmp_path) == []  # the partial file is not left lying around
    assert dump(path) == before and version(path) == 0  # nothing was migrated: not even the baseline
    monkeypatch.undo()
    result = migrations.run(make_engine(path))  # and the same start works once the disk is fine
    assert result["applied"] == [1, 2, 3, 4] and len(backups_of(tmp_path)) == 1


def test_a_backup_that_cannot_be_written_leaves_nothing_behind(tmp_path, monkeypatch):
    path = legacy_db(tmp_path)
    real_backup = migrations.backup_database

    class Failing:
        """A connection whose VACUUM INTO writes a partial file and then fails."""

        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *args):
            if sql.startswith("VACUUM INTO"):
                with open(args[0][0], "wb") as f:
                    f.write(b"partial")
                raise sqlite3.OperationalError("database or disk is full")
            return self._c.execute(sql, *args)

    monkeypatch.setattr(migrations, "backup_database", lambda conn, folder, v: real_backup(Failing(conn), folder, v))
    with pytest.raises(MigrationError, match="no partial backup was kept"):
        migrations.run(make_engine(path))
    assert backups_of(tmp_path) == []


def test_old_backups_are_pruned_before_a_migration_and_only_verifying_ones_count(tmp_path):
    path = legacy_db(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir()
    sample = legacy_db(tmp_path / "..", rows=3)  # a valid database file to copy from
    for n in range(7):  # 7 old valid backups, oldest first
        name = folder / f"pre-v2-2024010{n + 1}-120000.db"
        with open(sample, "rb") as src, open(name, "wb") as dst:
            dst.write(src.read())
        os.utime(name, (1_700_000_000 + n * 100, 1_700_000_000 + n * 100))
    broken = folder / "pre-v2-20250101-000000.db"  # the newest by name and time, but not a database
    broken.write_bytes(b"this is not a database, a crash left it")
    os.utime(broken, (1_800_000_000, 1_800_000_000))
    (folder / "notes.txt").write_text("not ours")

    migrations.run(make_engine(path))
    names = backups_of(tmp_path)
    assert "pre-v2-20250101-000000.db" not in names  # the broken one is gone: it is not a backup
    assert "notes.txt" in names  # nothing else in the folder is touched
    ours = [n for n in names if n != "notes.txt"]
    assert len(ours) == migrations.KEEP_BACKUPS  # the 4 newest valid old ones + the new one
    assert {f"pre-v2-2024010{n}-120000.db" for n in (4, 5, 6, 7)} <= set(ours)


def test_a_start_that_keeps_failing_makes_at_most_one_backup_per_attempt(tmp_path):
    path = legacy_db(tmp_path)
    before = dump(path)

    def boom(conn):
        raise RuntimeError("synthetic")

    failing = Migration(2, "fails", boom, foreign_keys_off=True)
    counts = []
    for _ in range(9):  # a crash-restart loop
        with pytest.raises(MigrationError):
            migrations.run(make_engine(path), [MIGRATIONS[0], failing])
        counts.append(len(backups_of(tmp_path)))
    assert counts[:5] == [1, 2, 3, 4, 5]  # one new backup per attempt ...
    assert max(counts) == migrations.KEEP_BACKUPS and counts[-1] == migrations.KEEP_BACKUPS  # ... never more than 5
    assert dump(path) == before


def test_several_migrations_in_one_start_share_one_backup(tmp_path):
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path), [*MIGRATIONS, V3])
    assert len(backups_of(tmp_path)) == 1


# --- two starts at once ------------------------------------------------------------------------------------


def test_a_start_that_loses_the_race_skips_the_migration_and_changes_nothing(tmp_path, monkeypatch):
    """Start A has decided to migrate (it read the version and took its backup) when start B completes
    the whole migration. A then gets the write lock, reads the version again inside the transaction,
    sees 3 and stands down: no second rebuild, no error, the data exactly as B left it."""
    path = legacy_db(tmp_path)
    real_backup = migrations.backup_database
    other = {}

    def backup_then_the_other_start_finishes(conn, folder, version_):
        made = real_backup(conn, folder, version_)
        if not other:  # only start A's backup triggers B (B's own run comes through here too)
            other["result"] = None
            monkeypatch.setattr(migrations, "backup_database", real_backup)
            other["result"] = migrations.run(make_engine(path))  # start B, complete
            other["dump"] = dump(path)
        return made

    monkeypatch.setattr(migrations, "backup_database", backup_then_the_other_start_finishes)
    result = migrations.run(make_engine(path))  # start A
    assert other["result"]["applied"] == [1, 2, 3, 4]
    assert result["applied"] == [] and result["from"] == 0
    assert version(path) == 4 and dump(path) == other["dump"]
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert shape(path) == fresh_shape(tmp_path)
