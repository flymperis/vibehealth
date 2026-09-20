"""Secrets at rest: encryption, key handling, redaction. Synthetic values only."""

import logging
import os

import pytest

from app import secret_store as ss

SYNTHETIC = "sk-synthetic-0123456789-abcdef"


def test_round_trip_and_ciphertext_shape():
    token = ss.encrypt(SYNTHETIC)
    assert token.startswith("enc:v1:") and SYNTHETIC not in token
    assert ss.is_encrypted(token)
    assert ss.decrypt(token) == SYNTHETIC
    assert ss.encrypt(SYNTHETIC) != token  # fresh IV each time


@pytest.mark.parametrize("bad", [None, "", "plain text", "enc:v1:", "enc:v1:not-a-token", "enc:v2:abc"])
def test_garbage_reads_as_unset(bad):
    assert ss.decrypt(bad) is None


def test_wrong_key_reads_as_unset_and_does_not_crash(monkeypatch, reload_config):
    monkeypatch.setenv("SECRET_KEY", "key-one-synthetic")
    reload_config()
    token = ss.encrypt(SYNTHETIC)
    assert ss.decrypt(token) == SYNTHETIC
    monkeypatch.setenv("SECRET_KEY", "key-two-synthetic")
    reload_config()
    assert ss.decrypt(token) is None


def test_env_key_wins_and_labels_are_separate(monkeypatch, reload_config, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "env-key-synthetic")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reload_config()
    assert ss.key_source() == "env"
    assert ss.derive(ss.LABEL_ENCRYPTION) != ss.derive(ss.LABEL_SESSION)
    assert ss.derive(ss.LABEL_SESSION) != b"env-key-synthetic"
    assert ss.session_signing_key() == ss.derive(ss.LABEL_SESSION)
    assert not os.path.exists(tmp_path / ".keys")  # no key file needed


def test_master_key_file_is_created_once_and_reused(monkeypatch, reload_config, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reload_config()
    assert ss.key_source() == "file"
    ss.ensure_key()
    path = tmp_path / ".keys" / "master.key"
    assert path.exists()
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (path.parent.stat().st_mode & 0o777) == 0o700
    first = path.read_bytes()
    token = ss.encrypt(SYNTHETIC)
    reload_config()  # a restart
    assert ss.decrypt(token) == SYNTHETIC
    assert path.read_bytes() == first


def test_empty_key_file_is_regenerated(monkeypatch, reload_config, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / ".keys").mkdir()
    (tmp_path / ".keys" / "master.key").write_bytes(b"")
    reload_config()
    ss.ensure_key()
    assert (tmp_path / ".keys" / "master.key").read_bytes().strip()


def test_last4_only_for_long_secrets():
    assert ss.last4(SYNTHETIC) == SYNTHETIC[-4:]
    assert ss.last4("short") == ""


def test_logs_never_show_a_secret(caplog):
    ss.install_log_redaction()
    ss.install_log_redaction()  # idempotent
    token = ss.encrypt(SYNTHETIC)
    log = logging.getLogger("vibehealth.test")
    with caplog.at_level(logging.DEBUG):
        log.info("token is %s", SYNTHETIC)
        log.warning("stored as %s", token)
        try:
            raise RuntimeError(f"failed with {SYNTHETIC}")
        except RuntimeError:
            log.exception("oops")
    assert SYNTHETIC not in caplog.text
    assert token not in caplog.text
    assert "***" in caplog.text


def test_redaction_leaves_other_records_alone_for_uvicorn_access_log():
    """uvicorn's formatter unpacks record.args: they must survive."""
    from uvicorn.logging import AccessFormatter

    ss.install_log_redaction()
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/health", "1.1", 200), None,
    )
    assert record.args is not None
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert "GET /api/health HTTP/1.1" in line


def test_traceback_with_a_secret_is_masked(caplog):
    ss.install_log_redaction()
    ss.register_secret(SYNTHETIC)
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError(SYNTHETIC)
        except ValueError:
            logging.getLogger("vibehealth.test").exception("failed")
    assert SYNTHETIC not in caplog.text and "ValueError" in caplog.text
