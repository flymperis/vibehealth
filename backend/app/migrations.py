"""Schema versions, tracked with SQLite's `PRAGMA user_version`.

How it works
- A database with no tables is new: `create_all` builds the current schema and
  it is stamped with the latest version.
- A database with tables and `user_version = 0` is the one that existed before
  this framework. Version 1 is the schema as it was then, so it is baselined
  (stamped) without touching the data, and later migrations run on top.
- Each migration runs in its own transaction together with its version bump: it
  either lands completely or not at all. A migration that rebuilds a table sets
  `foreign_keys_off=True`: `PRAGMA foreign_keys` cannot change inside a
  transaction, so the runner switches it off before the transaction begins,
  checks `PRAGMA foreign_key_check` before the commit, and switches it back on.
- Before the first migration that changes a database holding data, the whole
  database is copied to `<data dir>/backups/pre-v<N>-<timestamp>.db` with
  `VACUUM INTO` (one consistent file, WAL content included). The copy is then opened read-only,
  `PRAGMA integrity_check` must say ok and every table must have the row count of the source;
  a copy that fails either is deleted and the start is refused. One start attempt makes at most one.
- Old backups are pruned BEFORE a migration starts (the newest 4 that verify are kept, so that the new one makes 5; one that does
  not open or fails the integrity check is removed) and again after a successful start;
  nothing else in the folder is touched.
- A migration that rebuilds a table first checks that the table has exactly the shape it expects
  and refuses (naming the backup) if anything else hangs on it: an extra column, index, trigger or
  view. Unknown objects are never dropped.
- Two starts at once are safe: the version is read again inside the write transaction, and a start
  that finds the migration already done skips it.
- Running again when everything is applied changes nothing.

- A migration that fails (or leaves broken foreign keys) is rolled back completely and the app
  refuses to start (`MigrationError`), naming the backup taken before it.

Migrations only see a plain `sqlite3.Connection` (already inside the
transaction) and must not commit. After the migrations, `create_all` adds any
table that is still missing; it never alters an existing one.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from . import models  # noqa: F401  (registers the tables)

log = logging.getLogger("vibehealth")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]
    # True for a table rebuild: foreign keys are off while it runs.
    foreign_keys_off: bool = False
    # False for a migration that only stamps; no backup is taken for it.
    changes_data: bool = True


def _baseline(_conn: sqlite3.Connection) -> None:
    """Version 1 = the schema that existed before versioning. Nothing to change."""


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}


# Version 2: documents can also be uploaded. `documents` is rebuilt (SQLite cannot make a NOT NULL
# column nullable in place). The DDL is written out here on purpose: a migration must not change
# when the models do. It has to end up identical to what `create_all` builds for the models.
_DOCUMENTS_V2 = """
CREATE TABLE documents_new (
    id INTEGER NOT NULL,
    paperless_id INTEGER,
    title VARCHAR NOT NULL,
    kind VARCHAR(12) NOT NULL,
    doc_date DATE,
    ignored BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    source VARCHAR DEFAULT 'paperless' NOT NULL,
    original_filename VARCHAR,
    stored_path VARCHAR,
    mime_type VARCHAR,
    size_bytes INTEGER,
    sha256 VARCHAR,
    PRIMARY KEY (id)
)
"""
_DOCUMENTS_V2_INDEXES = (
    "CREATE INDEX ix_documents_source ON documents (source)",
    "CREATE UNIQUE INDEX ix_documents_paperless_id ON documents (paperless_id)",
    "CREATE UNIQUE INDEX ux_documents_sha256_upload ON documents (sha256) WHERE source = 'upload'",
    "CREATE INDEX ix_documents_ignored ON documents (ignored)",
)
_V1_COLUMNS = "id, paperless_id, title, kind, doc_date, ignored, created_at, updated_at"


# The `documents` table exactly as schema version 1 had it: (name, type, notnull, default, pk), and the
# only objects that belong to it. Anything else means someone changed the database by hand (or a newer
# release did): the rebuild would silently drop it, so it is refused instead.
_V1_TABLE_INFO = [
    ("id", "INTEGER", 1, None, 1),
    ("paperless_id", "INTEGER", 1, None, 0),
    ("title", "VARCHAR", 1, None, 0),
    ("kind", "VARCHAR(12)", 1, None, 0),
    ("doc_date", "DATE", 0, None, 0),
    ("ignored", "BOOLEAN", 1, None, 0),
    ("created_at", "DATETIME", 1, None, 0),
    ("updated_at", "DATETIME", 1, None, 0),
]
_V1_INDEXES = {  # name -> (unique, columns)
    "ix_documents_paperless_id": (1, ["paperless_id"]),
    "ix_documents_ignored": (0, ["ignored"]),
}


def _v1_shape_problems(conn: sqlite3.Connection) -> list[str]:
    """What differs from the v1 `documents` table: empty when it is exactly that."""
    problems: list[str] = []
    columns = [(r[1], (r[2] or "").upper(), r[3], r[4], r[5]) for r in conn.execute('PRAGMA table_info("documents")')]
    if columns != _V1_TABLE_INFO:
        have, want = {c[0] for c in columns}, {c[0] for c in _V1_TABLE_INFO}
        extra, missing = sorted(have - want), sorted(want - have)
        problems.append(
            "its columns are not the version 1 columns"
            + (f" (extra: {', '.join(extra)})" if extra else "")
            + (f" (missing: {', '.join(missing)})" if missing else "")
            + ("" if extra or missing else " (a type, default, NOT NULL or key differs)")
        )
    if conn.execute('PRAGMA foreign_key_list("documents")').fetchall():
        problems.append("it has a foreign key of its own")
    for kind, name, table, sql in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name = 'documents' OR name = 'documents_new'"
    ).fetchall():
        if kind == "table" and name == "documents":
            continue
        if kind == "index" and name in _V1_INDEXES and table == "documents":
            unique, want_cols = _V1_INDEXES[name]
            info = conn.execute('PRAGMA index_list("documents")').fetchall()
            flags = {r[1]: (r[2], r[4]) for r in info}  # name -> (unique, partial)
            cols = [r[2] for r in conn.execute(f'PRAGMA index_xinfo("{name}")') if r[5]]
            if flags.get(name) != (unique, 0) or cols != want_cols:
                problems.append(f"the index {name} is not the one version 1 had")
            continue
        problems.append(f"it has an unexpected {kind} named {name}")
    for kind, name, sql in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('view', 'trigger') AND sql IS NOT NULL"
    ).fetchall():
        if re.search(r"\bdocuments\b", sql, re.IGNORECASE) and not any(
            p.endswith(f"named {name}") for p in problems
        ):
            problems.append(f"the {kind} {name} refers to it")
    return problems


def _uploads(conn: sqlite3.Connection) -> None:
    """Runs with foreign keys OFF: with them on, dropping `documents` would delete every
    extracted value and run through ON DELETE CASCADE. Rows keep their ids, so the children
    still point at them. Checked before the runner commits: `foreign_key_check` is empty.
    Refuses (MigrationError, nothing touched) when `documents` is not exactly the version 1 table."""
    if "documents" not in _user_tables(conn):
        return  # nothing to rebuild: create_all builds the table
    if "source" in _columns(conn, "documents"):
        return  # already the new shape (nothing to do)
    problems = _v1_shape_problems(conn)
    if problems:
        raise MigrationError(
            "the table documents is not exactly what schema version 1 had, and rebuilding it would drop what "
            "is different: " + "; ".join(problems) + ". Nothing was dropped or changed"
        )
    before = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    conn.execute(_DOCUMENTS_V2)
    conn.execute(
        f"INSERT INTO documents_new ({_V1_COLUMNS}, source) SELECT {_V1_COLUMNS}, 'paperless' FROM documents"
    )
    copied = conn.execute("SELECT count(*) FROM documents_new").fetchone()[0]
    if copied != before:
        raise MigrationError(f"documents rebuild copied {copied} of {before} rows")
    conn.execute("DROP TABLE documents")
    conn.execute("ALTER TABLE documents_new RENAME TO documents")
    for statement in _DOCUMENTS_V2_INDEXES:
        conn.execute(statement)


# Version 3: text reports (imaging, medical opinions, prescriptions) keep their page text and an automatic
# summary. Two new tables and nothing else: no existing table is touched, so no rebuild, and a release without
# them still runs on the same database. Written out like version 2 (a migration must not change when the models
# do), it has to end up identical to what `create_all` builds; IF NOT EXISTS makes it harmless where the tables
# are already there.
_TEXT_REPORTS_V3 = (
    """
CREATE TABLE IF NOT EXISTS document_texts (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    page INTEGER NOT NULL,
    text VARCHAR NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
)
""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_document_texts_page ON document_texts (document_id, page)",
    """
CREATE TABLE IF NOT EXISTS document_reports (
    document_id INTEGER NOT NULL,
    summary_status VARCHAR NOT NULL,
    summary_error VARCHAR NOT NULL,
    conclusion VARCHAR NOT NULL,
    key_findings VARCHAR NOT NULL,
    auto_generated BOOLEAN NOT NULL,
    summary_model VARCHAR NOT NULL,
    lab_pages VARCHAR NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (document_id),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
)
""",
)


def _text_reports(conn: sqlite3.Connection) -> None:
    for statement in _TEXT_REPORTS_V3:
        conn.execute(statement)


# Version 4: what the kind-specific extraction of a text report found (a JSON column on the summary), how a reading
# went (`route`, which of the two pipelines read the document) and the folded text that search looks through. Two
# ADD COLUMNs (each with a default, so old rows are valid and nothing is rebuilt) and one new table. Written out
# like the others; each step is skipped where it is already there, so it is harmless on a database that has it.
_SEARCH_V4 = """
CREATE TABLE IF NOT EXISTS document_search (
    document_id INTEGER NOT NULL,
    body VARCHAR NOT NULL,
    PRIMARY KEY (document_id),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
)
"""


def _report_details(conn: sqlite3.Connection) -> None:
    # (A table that does not exist yet is left to `create_all`, which builds it with the column.)
    reports, runs = _columns(conn, "document_reports"), _columns(conn, "extraction_runs")
    if reports and "details" not in reports:
        conn.execute("ALTER TABLE document_reports ADD COLUMN details VARCHAR DEFAULT '{}' NOT NULL")
    if runs and "route" not in runs:
        conn.execute("ALTER TABLE extraction_runs ADD COLUMN route VARCHAR DEFAULT '' NOT NULL")
    conn.execute(_SEARCH_V4)


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", _baseline, changes_data=False),
    Migration(2, "uploads: documents source and file columns", _uploads, foreign_keys_off=True),
    Migration(3, "text reports: page text and automatic summary tables", _text_reports),
    Migration(4, "report details, reading route and search text", _report_details),
]


def latest_version(migrations: list[Migration] | None = None) -> int:
    return max(m.version for m in (migrations if migrations is not None else MIGRATIONS))


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def _has_data(conn: sqlite3.Connection, tables: list[str]) -> bool:
    for name in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
            return True
    return False


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for name in _user_tables(conn):
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
    return counts


def verify_backup(path: str, expected_counts: dict[str, int] | None = None) -> None:
    """Open a backup read-only: `PRAGMA integrity_check` must say ok and, when `expected_counts` is
    given, every table must hold exactly that many rows. Raises MigrationError otherwise."""
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise MigrationError(f"the backup does not open ({type(exc).__name__})") from None
    try:
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
            counts = _row_counts(con)
        except sqlite3.Error as exc:
            raise MigrationError(f"the backup cannot be read ({type(exc).__name__})") from None
        if verdict != "ok":
            raise MigrationError("the backup fails PRAGMA integrity_check")
        if expected_counts is not None and counts != expected_counts:
            raise MigrationError("the backup does not hold the same rows as the database")
    finally:
        con.close()


def backup_database(conn: sqlite3.Connection, backup_dir: str, version: int) -> str:
    """Write a consistent copy of the whole database and check it; returns its path. The copy is
    opened read-only afterwards: it must pass `PRAGMA integrity_check` and hold the same number of rows
    in every table as the source. If anything fails the partial file is deleted and the error raised
    (no file that looks like a backup but is not one is ever left behind)."""
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)  # medical data
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"pre-v{version}-{stamp}.db")
    n = 1
    while os.path.exists(path):  # VACUUM INTO refuses to overwrite
        n += 1
        path = os.path.join(backup_dir, f"pre-v{version}-{stamp}-{n}.db")
    # (Counted just before the copy. At start nothing else writes to the database; a second start that
    # does is stopped by the lock in the migration's own transaction.)
    expected = _row_counts(conn)
    try:
        conn.execute("VACUUM INTO ?", (path,))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        verify_backup(path, expected)
    except BaseException:
        for leftover in (path, path + "-journal", path + "-wal", path + "-shm"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    return path


KEEP_BACKUPS = 5
_BACKUP_NAME = re.compile(r"pre-v\d+-\d{8}-\d{6}(-\d+)?\.db")


def prune_backups(backup_dir: str, keep: int = KEEP_BACKUPS, verify: bool = False) -> list[str]:
    """Delete all but the newest `keep` of the app's own `pre-v<N>-<timestamp>.db` backups.
    Nothing else in the folder is touched: other names, folders and links are left alone.
    With `verify` only backups that open and pass `PRAGMA integrity_check` count towards `keep`; one
    that does not (a partial file from a crash, say) is removed, because it is not a backup."""
    try:
        entries = [e for e in os.scandir(backup_dir)
                   if _BACKUP_NAME.fullmatch(e.name) and e.is_file(follow_symlinks=False)]
    except OSError:
        return []
    entries.sort(key=lambda e: (e.stat(follow_symlinks=False).st_mtime_ns, e.name), reverse=True)
    removed = []
    kept = 0
    for entry in entries:
        if verify:
            try:
                verify_backup(entry.path)
                good = True
            except MigrationError:
                good = False
                log.warning("the backup %s does not verify: removing it", entry.name)
        else:
            good = True
        if good and kept < keep:
            kept += 1
            continue
        try:
            os.remove(entry.path)
            removed.append(entry.name)
        except OSError:
            log.warning("could not remove the old backup %s", entry.name)
    return removed


def _run_one(conn: sqlite3.Connection, m: Migration) -> bool:
    """Apply one migration in its own transaction. False when it was skipped because another start
    had already applied it (the version is read again once the write lock is held)."""
    # foreign_keys is a no-op inside a transaction: set it before BEGIN.
    conn.execute(f"PRAGMA foreign_keys={'OFF' if m.foreign_keys_off else 'ON'}")
    wanted = 0 if m.foreign_keys_off else 1
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != wanted:
        # Without this a rebuild would cascade-delete the extracted values: never go on.
        raise MigrationError(f"could not set foreign_keys for migration {m.version} ({m.name})")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _user_version(conn) >= m.version:  # a concurrent start got here first: nothing left to do
            conn.execute("ROLLBACK")
            return False
        m.apply(conn)
        if m.foreign_keys_off and conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError(f"migration {m.version} ({m.name}) left broken foreign keys")
        conn.execute(f"PRAGMA user_version={int(m.version)}")
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def run(engine: Engine, migrations: list[Migration] | None = None) -> dict:
    """Bring the database to the latest version. Safe to call on every start."""
    migrations = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m.version)
    latest = latest_version(migrations)
    db_path = engine.url.database
    result: dict = {"from": 0, "to": latest, "applied": [], "backup": None, "fresh": False}

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        current = _user_version(conn)
        result["from"] = current
        if current > latest:
            raise MigrationError(
                f"database is at schema version {current}, this app only knows up to {latest}: "
                "it was written by a newer VibeHealth. Refusing to start."
            )

        tables = _user_tables(conn)
        if not tables:
            SQLModel.metadata.create_all(engine)
            conn.execute(f"PRAGMA user_version={latest}")
            result["fresh"] = True
            log.info("database created at schema version %s", latest)
            return result

        pending = [m for m in migrations if m.version > current]
        if pending and any(m.changes_data for m in pending) and _has_data(conn, tables):
            first = next(m for m in pending if m.changes_data)
            backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
            # Old backups go first (only ones that verify count), leaving room for exactly one new
            # backup for this attempt: a start that keeps failing never holds more than KEEP_BACKUPS.
            prune_backups(backup_dir, keep=KEEP_BACKUPS - 1, verify=True)
            try:
                result["backup"] = backup_database(conn, backup_dir, first.version)
            except Exception as exc:
                detail = f": {exc}" if isinstance(exc, MigrationError) else ""
                raise MigrationError(
                    f"Could not make the backup that comes before migration {first.version} "
                    f"({type(exc).__name__}{detail}). Nothing was changed and no partial backup was kept. "
                    "Refusing to start: check the free space and the permissions of the data folder."
                ) from exc
            log.info("database backed up to %s before migrating", os.path.basename(result["backup"]))
        for m in pending:
            try:
                done = _run_one(conn, m)
            except Exception as exc:
                where = (
                    f"A copy of the database from before is in {result['backup']}. "
                    if result["backup"] else "No backup was needed: the database held no data. "
                )
                # our own refusals say why (they hold no data); any other exception is named by class only
                why = f"{type(exc).__name__}: {exc}" if isinstance(exc, MigrationError) else type(exc).__name__
                raise MigrationError(
                    f"Migration {m.version} ({m.name}) failed ({why}) and was rolled back, "
                    f"so the database is unchanged at schema version {_user_version(conn)}. {where}"
                    "Refusing to start: fix the cause (see the log) or restore the backup."
                ) from exc
            if not done:
                log.info("migration %s (%s) was already applied by another start: skipped", m.version, m.name)
                continue
            result["applied"].append(m.version)
            log.info("database migrated to version %s (%s)", m.version, m.name)
    finally:
        conn.close()

    SQLModel.metadata.create_all(engine)  # tables added since; never alters existing ones
    removed = prune_backups(os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups"))
    if removed:
        log.info("removed %d old database backup(s), keeping the newest %d", len(removed), KEEP_BACKUPS)
    return result
