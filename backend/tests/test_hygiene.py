"""Master key creation never overwrites (L6) and old pre-migration backups are pruned safely (L7)."""

import errno
import os
import shutil
import threading

import pytest

from app import migrations, secret_store
from app.config import get_settings
from app.db import engine


# --- L6: the master key ---------------------------------------------------------------------------------------


@pytest.fixture
def key_path(tmp_path):
    return str(tmp_path / ".keys" / "master.key")


def test_a_key_that_exists_is_never_truncated_or_replaced(key_path):
    first = secret_store._create_key_file(key_path)
    with open(key_path, "rb") as f:
        assert f.read().strip() == first
    for _ in range(3):
        again = secret_store._create_key_file(key_path)  # called again: returns the existing key
        assert again == first
    with open(key_path, "rb") as f:
        assert f.read().strip() == first


def test_creation_never_opens_with_truncate(key_path, monkeypatch):
    flags = []
    real = os.open

    def spy(path, flag, *a, **kw):
        flags.append(flag)
        return real(path, flag, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    secret_store._create_key_file(key_path)
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "wb") as f:  # an empty leftover, then a second creation over it
        f.write(b"")
    secret_store._create_key_file(key_path)
    assert flags and all(not flag & getattr(os, "O_TRUNC", 0) for flag in flags)
    assert all(flag & os.O_EXCL for flag in flags)


def test_concurrent_creation_gives_every_process_the_same_key(key_path):
    results = []
    barrier = threading.Barrier(12)

    def go():
        barrier.wait()
        results.append(secret_store._create_key_file(key_path))

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 12 and len(set(results)) == 1
    with open(key_path, "rb") as f:
        assert f.read().strip() == results[0]
    assert os.listdir(os.path.dirname(key_path)) == ["master.key"]  # no temp files left behind


def test_an_empty_leftover_is_replaced_once(key_path):
    os.makedirs(os.path.dirname(key_path))
    open(key_path, "wb").close()
    made = secret_store._create_key_file(key_path)
    assert made and secret_store._create_key_file(key_path) == made
    assert os.listdir(os.path.dirname(key_path)) == ["master.key"]


def test_it_still_works_where_hard_links_are_unavailable(key_path, monkeypatch):
    def no_links(src, dst, **kw):
        raise OSError(errno.ENOTSUP, "no hard links here")

    monkeypatch.setattr(os, "link", no_links)
    made = secret_store._create_key_file(key_path)
    with open(key_path, "rb") as f:
        assert f.read().strip() == made
    assert secret_store._create_key_file(key_path) == made  # and still refuses to overwrite
    assert os.listdir(os.path.dirname(key_path)) == ["master.key"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_key_file_and_folder_are_private(key_path):
    secret_store._create_key_file(key_path)
    assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(key_path)).st_mode & 0o777) == "0o700"


# --- L7: pruning the pre-migration backups ------------------------------------------------------------------------


def make(dir_, name, age):
    path = os.path.join(dir_, name)
    with open(path, "wb") as f:
        f.write(b"x")
    stamp = 1_700_000_000 - age
    os.utime(path, (stamp, stamp))
    return path


def test_only_the_newest_five_backups_are_kept_and_nothing_else_is_touched(tmp_path):
    d = str(tmp_path)
    ours = [make(d, f"pre-v{n}-20250101-0000{n:02d}.db", age=(9 - n) * 100) for n in range(1, 9)]  # 1 is the oldest
    ours.append(make(d, "pre-v2-20250101-000001-2.db", age=5000))  # the "-2" suffix form, oldest of all
    others = [
        make(d, "manual-backup.db", age=99_999),
        make(d, "pre-v1.db", age=99_999),               # not our naming
        make(d, "pre-v1-latest.db", age=99_999),
        make(d, "pre-v1-20250101-000001.db.bak", age=99_999),
        make(d, "notes.txt", age=99_999),
        make(d, "vibehealth.db", age=99_999),
    ]
    os.mkdir(os.path.join(d, "pre-v9-20250101-000001.db"))  # a folder with our name: not a file, left alone
    removed = migrations.prune_backups(d)
    assert len(removed) == 4
    kept = sorted(n for n in os.listdir(d) if migrations._BACKUP_NAME.fullmatch(n) and os.path.isfile(os.path.join(d, n)))
    assert len(kept) == 5
    assert kept == sorted(f"pre-v{n}-20250101-0000{n:02d}.db" for n in range(4, 9))  # the newest five
    for path in others:
        assert os.path.exists(path)
    assert os.path.isdir(os.path.join(d, "pre-v9-20250101-000001.db"))


def test_fewer_than_five_are_all_kept_and_a_missing_folder_is_fine(tmp_path):
    d = str(tmp_path)
    make(d, "pre-v1-20250101-000001.db", 10)
    make(d, "pre-v1-20250101-000002.db", 5)
    assert migrations.prune_backups(d) == []
    assert migrations.prune_backups(os.path.join(d, "nope")) == []
    assert len(os.listdir(d)) == 2


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_are_never_followed_or_removed(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    outside = tmp_path / "precious.db"
    outside.write_bytes(b"data")
    for n in range(7):
        make(str(d), f"pre-v1-20250101-00000{n}.db", age=1000 - n)
    link = d / "pre-v1-20240101-000000.db"
    link.symlink_to(outside)
    migrations.prune_backups(str(d))
    assert outside.exists() and link.is_symlink()


def test_startup_migration_run_prunes(tmp_path):
    backups = os.path.join(os.path.dirname(os.path.abspath(get_settings().database_path)), "backups")
    os.makedirs(backups, exist_ok=True)
    created = [make(backups, f"pre-v1-20240101-0000{n:02d}.db", age=(10 - n) * 100) for n in range(1, 9)]
    bystander = make(backups, "keep-me.db", age=10**6)
    try:
        migrations.run(engine)
        left = [p for p in created if os.path.exists(p)]
        assert left == created[-5:]
        assert os.path.exists(bystander)
    finally:
        for p in created + [bystander]:
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(backups, ignore_errors=True)


def test_a_fresh_backup_is_kept_and_counted_among_the_five(tmp_path):
    import sqlite3

    d = str(tmp_path)
    for n in range(5):
        make(d, f"pre-v1-20240101-00000{n}.db", age=1000 - n)
    conn = sqlite3.connect(":memory:")
    path = migrations.backup_database(conn, d, 2)
    conn.close()
    assert os.path.exists(path)
    migrations.prune_backups(d)
    names = [n for n in os.listdir(d) if migrations._BACKUP_NAME.fullmatch(n)]
    assert len(names) == 5 and os.path.basename(path) in names
