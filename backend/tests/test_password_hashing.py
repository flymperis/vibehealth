"""scrypt profile and transparent rehash (L1), fail-closed on an unreadable saved hash (L2),
and redaction of environment secrets in real log output (L3)."""

import base64
import hashlib
import io
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import secret_store, security, settings_store
from app.db import engine
from app.main import app
from app.models import AppSetting

PASSWORD = "correct horse battery"


def new_client() -> TestClient:
    return TestClient(app)


def login(c, password=PASSWORD):
    return c.post("/api/auth/login", json={"password": password})


def pbkdf2(password: str, iterations: int = 240_000, salt: bytes = b"0123456789abcdef") -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def old_scrypt(password: str, n=2**9, r=8, p=1, salt: bytes = b"0123456789abcdef") -> str:
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * r * (n + p + 2) + 2**20)
    return "scrypt${}${}${}${}${}".format(n, r, p, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def save_hash(value: str) -> None:
    settings_store.update("auth", {"password_hash": value, "session_epoch": settings_store.value("auth", "session_epoch") + 1})


# --- L1: the profile ---------------------------------------------------------------------------------------


@pytest.mark.real_scrypt
def test_production_profile_is_owasp_strength_and_fast_enough():
    assert (security._SCRYPT_N, security._SCRYPT_R, security._SCRYPT_P) == (2**15, 8, 3)
    started = time.perf_counter()
    stored = security.hash_password(PASSWORD)
    hashing = time.perf_counter() - started
    assert stored.startswith("scrypt$32768$8$3$")
    started = time.perf_counter()
    assert security.verify_password(PASSWORD, stored) and not security.verify_password("nope", stored)
    verifying = (time.perf_counter() - started) / 2
    print(f"scrypt N=2^15 r=8 p=3: hash {hashing:.3f} s, verify {verifying:.3f} s")
    assert verifying < 1.5  # a login must not feel slow (about 0.2 s on a normal machine)


@pytest.mark.real_scrypt
def test_the_largest_hash_we_accept_still_verifies_within_memory():
    stored = old_scrypt(PASSWORD, n=2**17, r=8, p=1)
    assert security.verify_password(PASSWORD, stored)


def test_needs_rehash(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_P", 3)
    cur = security.hash_password(PASSWORD)
    assert not security.needs_rehash(cur)
    assert security.needs_rehash(pbkdf2(PASSWORD))
    assert security.needs_rehash(old_scrypt(PASSWORD, n=2**9))
    assert security.needs_rehash(old_scrypt(PASSWORD, n=security._SCRYPT_N, r=4, p=1))
    assert security.needs_rehash(old_scrypt(PASSWORD, n=security._SCRYPT_N, r=8, p=2))
    stronger = old_scrypt(PASSWORD, n=security._SCRYPT_N * 2, r=8, p=security._SCRYPT_P)
    assert not security.needs_rehash(stronger)  # never weaken a hash made by hand
    for junk in ("", "x", "scrypt", "scrypt$a$b$c$d$e", "md5$1$2$3", "locked$unreadable"):
        assert not security.needs_rehash(junk)


def test_login_upgrades_a_saved_pbkdf2_hash_without_ending_sessions():
    save_hash(pbkdf2(PASSWORD))
    phone = new_client()
    assert login(phone).status_code == 200  # this login upgrades it
    stored = security.password_hash()
    assert stored.startswith("scrypt$") and not security.needs_rehash(stored)
    assert security.verify_password(PASSWORD, stored) and not security.verify_password("nope", stored)
    assert phone.get("/api/documents").status_code == 200
    epoch = security.session_epoch()
    laptop = new_client()
    assert login(laptop).status_code == 200  # nothing left to upgrade: hash and epoch stay
    assert security.password_hash() == stored and security.session_epoch() == epoch
    assert phone.get("/api/documents").status_code == 200  # earlier sessions survive the upgrade
    assert security._salt_of(stored) == security._salt_of(pbkdf2(PASSWORD))  # same salt: the fingerprint holds


def test_login_upgrades_an_older_scrypt_hash():
    save_hash(old_scrypt(PASSWORD, n=2**9))
    other = new_client()
    assert login(other).status_code == 200
    n, r, p = security.password_hash().split("$")[1:4]
    assert (int(n), int(r), int(p)) == (security._SCRYPT_N, security._SCRYPT_R, security._SCRYPT_P)


def test_a_wrong_password_never_upgrades_anything():
    legacy = pbkdf2(PASSWORD)
    save_hash(legacy)
    assert login(new_client(), "wrong").status_code == 401
    assert security.password_hash() == legacy


def test_an_environment_hash_is_not_rewritten(monkeypatch, reload_config):
    legacy = pbkdf2(PASSWORD)
    monkeypatch.setenv("APP_PASSWORD_HASH", legacy)
    reload_config()
    assert login(new_client()).status_code == 200
    assert security.password_hash() == legacy
    assert settings_store.resolve("auth", use_cache=False).sources["password_hash"] == "env"
    with Session(engine) as s:
        assert s.get(AppSetting, "auth.password_hash") is None  # no shadowing row


def test_a_failed_upgrade_does_not_fail_the_login(monkeypatch):
    save_hash(pbkdf2(PASSWORD))
    real = settings_store.update

    def boom(section, changes, **kw):
        if "password_hash" in changes:
            raise RuntimeError("disk full")
        return real(section, changes, **kw)

    monkeypatch.setattr(settings_store, "update", boom)
    assert login(new_client()).status_code == 200
    assert security.password_hash().startswith("pbkdf2_sha256$")  # retried at the next login


def test_an_upgrade_does_not_overwrite_a_password_changed_meanwhile():
    legacy = pbkdf2(PASSWORD)
    save_hash(legacy)
    newer = security.hash_password("a different password")
    save_hash(newer)
    security.maybe_rehash(PASSWORD, legacy)  # a slow login that verified the old hash
    assert security.password_hash() == newer


def test_login_answers_are_unchanged_for_current_hashes(monkeypatch):
    save_hash(security.hash_password(PASSWORD))
    before = security.password_hash()
    assert login(new_client()).status_code == 200 and security.password_hash() == before


# --- L2: an unreadable saved hash locks the app -------------------------------------------------------------------


def put_raw(value: str) -> None:
    with Session(engine) as s:
        row = s.get(AppSetting, "auth.password_hash") or AppSetting(key="auth.password_hash")
        row.value = value
        s.add(row)
        s.commit()
    settings_store.clear_cache()


@pytest.mark.parametrize("raw", ["{not json", "", "123", "null", "[1, 2]", '{"a": 1}', "\x00\x01"])
def test_an_unreadable_saved_hash_locks_the_app(raw, caplog):
    caplog.set_level(logging.ERROR, logger="vibehealth")
    security._last_locked_log = -1e9
    put_raw(raw)
    c = new_client()
    status = c.get("/api/auth/status").json()
    assert status["password_required"] is True and status["authenticated"] is False and status["mode"] == "password"
    assert c.get("/api/documents").status_code == 401
    assert c.get("/api/settings").status_code == 401
    assert login(c, "anything at all").status_code == 401
    assert c.post("/api/auth/change-password", json={"new": "n" * 12, "setup_code": "ABCD2345"}).status_code == 401
    assert "LOCKED" in caplog.text and "reset-password" in caplog.text


def test_env_hash_does_not_rescue_an_unreadable_saved_row(monkeypatch, reload_config):
    put_raw("{broken")
    monkeypatch.setenv("APP_PASSWORD_HASH", security.hash_password(PASSWORD))
    reload_config()
    put_raw("{broken")
    assert login(new_client()).status_code == 401  # locked, not falling back to the env hash


def test_reset_password_recovers_a_locked_app():
    put_raw("{broken")
    assert new_client().get("/api/documents").status_code == 401
    security.reset_password()
    assert not security.password_hash()
    assert new_client().get("/api/documents").status_code == 200
    code = security.current_setup_code()
    r = new_client().post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": code})
    assert r.status_code == 200


def test_a_locked_app_leaves_no_setup_code_lying_around():
    security.new_setup_code(announce=False)
    put_raw("{broken")
    security.ensure_setup_code()
    import os

    assert not os.path.exists(security.setup_code_path())


def test_a_readable_hash_and_an_absent_row_behave_as_before():
    assert security.password_hash() == ""  # absent: open
    save_hash(security.hash_password(PASSWORD))
    assert security.password_hash().startswith("scrypt$")


# --- L3: environment secrets are masked in real log output -------------------------------------------------------------


@pytest.fixture
def captured():
    """A real handler with a real formatter, the way the app logs."""
    secret_store.install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("vibehealth")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(old)


def test_env_secrets_are_registered_and_masked(monkeypatch, reload_config, captured):
    token = "env-paperless-token-0123456789"
    pw_hash = security.hash_password(PASSWORD)
    key = "env-secret-key-abcdefghijklmnop"
    monkeypatch.setenv("PAPERLESS_TOKEN", token)
    monkeypatch.setenv("APP_PASSWORD_HASH", pw_hash)
    monkeypatch.setenv("SECRET_KEY", key)
    reload_config()
    secret_store.register_env_secrets()
    log = logging.getLogger("vibehealth")
    log.info("connecting with token %s", token)
    log.warning("hash is %s and key %s", pw_hash, key)
    try:
        raise RuntimeError(f"failed with {token}")
    except RuntimeError:
        log.exception("boom %s", key)
    out = captured.getvalue()
    assert out.count("***") >= 4
    for secret in (token, pw_hash, key):
        assert secret not in out
    assert "connecting with token ***" in out and "boom ***" in out


def test_app_startup_registers_env_secrets_before_anything_is_logged(monkeypatch, reload_config, captured):
    monkeypatch.setenv("PAPERLESS_TOKEN", "startup-token-9876543210zz")
    reload_config()
    with TestClient(app):
        logging.getLogger("vibehealth").info("first line with startup-token-9876543210zz in it")
    assert "startup-token-9876543210zz" not in captured.getvalue()


def test_a_short_secret_key_is_warned_about_without_being_shown(monkeypatch, reload_config, captured):
    monkeypatch.setenv("SECRET_KEY", "short-key-15chr")  # 15 characters
    reload_config()
    secret_store.register_env_secrets()
    out = captured.getvalue()
    assert "SECRET_KEY is only 15 characters" in out and "short-key-15chr" not in out


def test_a_long_enough_secret_key_is_not_warned_about(monkeypatch, reload_config, captured):
    monkeypatch.setenv("SECRET_KEY", "a-key-of-16-chars")
    reload_config()
    secret_store.register_env_secrets()
    assert "SECRET_KEY is only" not in captured.getvalue()
    monkeypatch.delenv("SECRET_KEY")
    reload_config()
    secret_store.register_env_secrets()  # not set at all (the key file is used): nothing to say
    assert "SECRET_KEY is only" not in captured.getvalue()


def put_raw_key(key: str, value: str) -> None:
    with Session(engine) as s:
        row = s.get(AppSetting, key) or AppSetting(key=key)
        row.value = value
        s.add(row)
        s.commit()
    settings_store.clear_cache()


@pytest.mark.parametrize("key", ["auth.session_epoch", "auth.revoked_sessions"])
@pytest.mark.parametrize("raw", ["{not json", '"text"', "-3", "[1]"])
def test_a_broken_epoch_or_revocation_row_locks_the_app_too(key, raw, caplog):
    """Falling back to epoch 0 / nothing revoked would revive ended sessions: fail closed instead."""
    caplog.set_level(logging.ERROR, logger="vibehealth")
    security._last_locked_log = -1e9
    owner = new_client()
    code = security.current_setup_code()
    assert owner.post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": code}).status_code == 200
    assert owner.get("/api/documents").status_code == 200
    put_raw_key(key, raw)
    assert owner.get("/api/documents").status_code == 401  # this session is not honoured any more
    assert login(new_client()).status_code == 401
    assert new_client().get("/api/auth/status").json()["authenticated"] is False
    assert "LOCKED" in caplog.text and key.split(".")[1] in caplog.text
    security.reset_password()  # the recovery path clears it
    assert not security.password_hash()
    assert settings_store.resolve("auth", use_cache=False).broken - {"session_epoch"} == frozenset()
