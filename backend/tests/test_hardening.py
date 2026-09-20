"""A private data folder (POSIX permissions) and the one-worker rule."""

import os

import pytest
from fastapi.testclient import TestClient

from app import hardening
from app.config import get_settings
from app.main import app

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


def mode(path) -> str:
    return oct(os.stat(path).st_mode & 0o777)


# --- permissions ---------------------------------------------------------------------------------------------


@POSIX_ONLY
def test_the_umask_is_private_before_any_file_is_made(tmp_path):
    old = os.umask(0o022)  # a launcher's usual umask
    try:
        hardening.apply_umask()
        made = tmp_path / "new-file"
        made.write_text("x")
        folder = tmp_path / "new-folder"
        folder.mkdir()
        assert mode(made) == "0o600" and mode(folder) == "0o700"
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(old)


@POSIX_ONLY
def test_secure_data_dir_tightens_everything_that_exists(tmp_path):
    data = tmp_path / "data"
    layout = ["uploads/ab", "uploads/.tmp", "cache/thumbs", "backups", ".keys"]
    for rel in layout:
        (data / rel).mkdir(parents=True)
        os.chmod(data / rel, 0o755)
    files = ["vibehealth.db", "vibehealth.db-wal", "vibehealth.db-shm", "uploads/ab/x.pdf", "cache/thumbs/t.jpg",
             "backups/pre-v2-20250101-000000.db", ".keys/master.key"]
    for rel in files:
        (data / rel).write_bytes(b"x")
        os.chmod(data / rel, 0o644)
    os.chmod(data, 0o755)

    assert hardening.secure_data_dir(str(data)) == []

    for rel in ["", *layout]:
        assert mode(data / rel) == "0o700", rel
    for rel in files:
        assert mode(data / rel) == "0o600", rel
    assert hardening.secure_data_dir(str(data)) == []  # and again: nothing to do


@POSIX_ONLY
def test_secure_data_dir_tolerates_a_failing_chmod_and_a_missing_folder(tmp_path, monkeypatch, caplog):
    assert hardening.secure_data_dir(str(tmp_path / "nowhere")) == []
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    os.chmod(data / "uploads", 0o755)

    def refuse(*a, **kw):
        raise PermissionError("a volume that refuses chmod")

    monkeypatch.setattr(os, "chmod", refuse)
    with caplog.at_level("WARNING", logger="vibehealth"):
        problems = hardening.secure_data_dir(str(data))
    assert problems and "could not make" in caplog.text  # a warning, not an exception


@POSIX_ONLY
def test_symbolic_links_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    data = tmp_path / "data"
    data.mkdir()
    os.symlink(outside, data / "uploads")
    hardening.secure_data_dir(str(data))
    assert mode(outside) == "0o755"  # what a link points at is not ours to change


@POSIX_ONLY
def test_after_start_the_real_data_folder_is_private():
    """Through the real start-up (lifespan): the folder, the sub-folders that exist, and the database files."""
    data = os.path.abspath(get_settings().data_dir)
    os.makedirs(os.path.join(data, "uploads", "zz"), exist_ok=True)
    os.chmod(os.path.join(data, "uploads", "zz"), 0o755)
    os.chmod(data, 0o755)
    with TestClient(app) as c:
        c.get("/api/health")
        assert mode(data) == "0o700"
        for name in ("uploads", "cache", "backups", ".keys"):
            if os.path.isdir(os.path.join(data, name)):
                assert mode(os.path.join(data, name)) == "0o700", name
        assert mode(os.path.join(data, "uploads", "zz")) == "0o700"
        db = get_settings().database_path
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                assert mode(db + suffix) == "0o600", suffix


def test_on_a_platform_without_posix_permissions_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(hardening, "_POSIX", False)
    (tmp_path / "uploads").mkdir()
    assert hardening.secure_data_dir(str(tmp_path)) == []
    hardening.apply_umask()  # no error, no effect


# --- one worker ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("environ,argv,expected", [
    ({}, ["uvicorn", "app.main:app"], 1),
    ({"WEB_CONCURRENCY": "4"}, [], 4),
    ({"WEB_CONCURRENCY": " 2 "}, [], 2),
    ({"WEB_CONCURRENCY": "lots"}, [], 1),
    ({"UVICORN_WORKERS": "3"}, [], 3),
    ({}, ["uvicorn", "app.main:app", "--workers", "5"], 5),
    ({}, ["uvicorn", "app.main:app", "--workers=6"], 6),
    ({}, ["gunicorn", "-w", "3", "app.main:app"], 3),
    ({}, ["uvicorn", "--workers"], 1),
    ({}, ["uvicorn", "--workers", "many"], 1),
    ({"WEB_CONCURRENCY": "2"}, ["uvicorn", "--workers", "7"], 7),
    ({"WEB_CONCURRENCY": "0"}, [], 1),
])
def test_declared_workers(environ, argv, expected):
    assert hardening.declared_workers(environ, argv) == expected


def test_more_than_one_worker_refuses_to_start(monkeypatch, caplog):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with caplog.at_level("CRITICAL", logger="vibehealth"):
        with pytest.raises(RuntimeError, match="ONE process"):
            with TestClient(app):
                pass
    assert "Refusing to start" in caplog.text


def test_the_refusal_can_be_turned_into_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setenv("VIBEHEALTH_ALLOW_MULTIPLE_WORKERS", "1")
    with caplog.at_level("WARNING", logger="vibehealth"):
        with TestClient(app) as c:
            assert c.get("/api/health").status_code == 200
    assert "continuing" in caplog.text


def test_one_worker_starts_normally(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
