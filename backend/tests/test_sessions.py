"""Session lifecycle: tokens are tied to the password in force, logout revokes, logout-all ends everything."""

import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app import security, settings_store
from app.main import app

PASSWORD = "correct horse battery"
OTHER = "another synthetic pass"


def new_client() -> TestClient:
    return TestClient(app)


def login(c, password=PASSWORD):
    return c.post("/api/auth/login", json={"password": password})


@pytest.fixture
def owner():
    c = new_client()
    r = c.post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": security.current_setup_code()})
    assert r.status_code == 200
    return c


def token_of(c):
    return c.cookies.get(security.COOKIE_NAME)


def with_token(token):
    c = new_client()
    c.cookies.set(security.COOKIE_NAME, token)
    return c


def test_token_carries_epoch_fingerprint_and_a_unique_id(owner):
    data = security._serializer().loads(token_of(owner))
    assert data["v"] == 2 and data["e"] == security.session_epoch()
    assert data["h"] == security._fingerprint(security.password_hash())
    assert security.password_hash() not in token_of(owner)
    other = new_client()
    login(other)
    assert security._serializer().loads(token_of(other))["j"] != data["j"]


def test_a_token_without_a_fingerprint_is_refused(owner):
    old = security._serializer().dumps({"v": 1, "e": security.session_epoch()})
    assert with_token(old).get("/api/documents").status_code == 401


def test_rotating_the_database_hash_ends_sessions_even_without_an_epoch_bump(owner):
    assert owner.get("/api/documents").status_code == 200
    settings_store.update("auth", {"password_hash": security.hash_password(OTHER)})  # epoch untouched
    assert owner.get("/api/documents").status_code == 401
    assert login(owner, OTHER).status_code == 200 and owner.get("/api/documents").status_code == 200


def test_rotating_app_password_hash_in_the_environment_ends_sessions(monkeypatch, reload_config):
    monkeypatch.setenv("APP_PASSWORD_HASH", security.hash_password(PASSWORD))
    reload_config()
    c = new_client()
    assert login(c).status_code == 200 and c.get("/api/documents").status_code == 200
    token = token_of(c)
    monkeypatch.setenv("APP_PASSWORD_HASH", security.hash_password(OTHER))  # the owner rotated it
    reload_config()
    assert with_token(token).get("/api/documents").status_code == 401
    assert login(new_client(), PASSWORD).status_code == 401
    fresh = new_client()
    assert login(fresh, OTHER).status_code == 200 and fresh.get("/api/documents").status_code == 200


def test_the_same_env_hash_keeps_sessions_across_a_restart(monkeypatch, reload_config):
    stored = security.hash_password(PASSWORD)
    monkeypatch.setenv("APP_PASSWORD_HASH", stored)
    reload_config()
    c = new_client()
    login(c)
    reload_config()  # a restart: same key, same hash
    assert c.get("/api/documents").status_code == 200


# --- logout ---------------------------------------------------------------------------------------------------


def test_logout_makes_the_token_itself_unusable(owner):
    token = token_of(owner)
    other_device = new_client()
    login(other_device)
    assert owner.post("/api/auth/logout").status_code == 200
    assert owner.get("/api/documents").status_code == 401  # the cookie is gone
    assert with_token(token).get("/api/documents").status_code == 401  # and a copy of the token is dead
    assert with_token(token).get("/api/auth/status").json()["authenticated"] is False
    assert other_device.get("/api/documents").status_code == 200  # other sessions are not touched


def test_logout_without_or_with_a_bad_token_is_harmless(owner):
    assert new_client().post("/api/auth/logout").status_code == 200
    c = with_token("garbage")
    assert c.post("/api/auth/logout").status_code == 200
    assert settings_store.value("auth", "revoked_sessions") == {}
    assert owner.get("/api/documents").status_code == 200


def test_revoked_list_forgets_only_expired_entries(owner):
    past = int(time.time()) - 10
    settings_store.update("auth", {"revoked_sessions": {"old-one": past, "old-two": past}})
    c = new_client()
    login(c)
    c.post("/api/auth/logout")
    kept = settings_store.value("auth", "revoked_sessions")
    assert set(kept).isdisjoint({"old-one", "old-two"}) and len(kept) == 1


def test_a_full_revoked_list_never_forgets_a_valid_revocation(owner, monkeypatch):
    """At the cap nothing still valid is evicted (that would revive a signed-out token): every session
    ends instead (the epoch moves), so all the tokens stay dead."""
    monkeypatch.setattr(security, "MAX_REVOKED", 3)
    tokens = []
    for _ in range(3):
        c = new_client()
        login(c)
        tokens.append(token_of(c))
        c.post("/api/auth/logout")
    assert len(settings_store.value("auth", "revoked_sessions")) == 3
    epoch = security.session_epoch()
    c = new_client()
    login(c)
    tokens.append(token_of(c))
    c.post("/api/auth/logout")  # one too many
    assert security.session_epoch() == epoch + 1
    assert all(with_token(t).get("/api/documents").status_code == 401 for t in tokens)
    assert login(new_client()).status_code == 200  # signing in again works


# --- logout-all -----------------------------------------------------------------------------------------------


def test_logout_all_ends_every_session_and_a_new_login_works(owner):
    phone, laptop = new_client(), new_client()
    login(phone)
    login(laptop)
    tokens = [token_of(c) for c in (owner, phone, laptop)]
    epoch = security.session_epoch()
    r = laptop.post("/api/auth/logout-all")
    assert r.status_code == 200 and security.COOKIE_NAME in r.headers["set-cookie"]
    assert security.session_epoch() == epoch + 1
    for t in tokens:
        assert with_token(t).get("/api/documents").status_code == 401
    assert owner.get("/api/documents").status_code == 401
    assert login(owner).status_code == 200 and owner.get("/api/documents").status_code == 200
    assert settings_store.value("auth", "revoked_sessions") == {}


def test_logout_all_needs_a_session(owner):
    assert new_client().post("/api/auth/logout-all").status_code == 401
    assert owner.get("/api/documents").status_code == 200  # nothing happened


def test_logout_all_is_a_state_change_so_it_is_origin_checked(owner):
    r = owner.post("/api/auth/logout-all", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert owner.get("/api/documents").status_code == 200


def test_forged_tokens_with_a_matching_shape_are_refused(owner):
    forged = URLSafeTimedSerializer("dev-insecure", salt="session").dumps(
        {"v": 2, "e": security.session_epoch(), "h": "0" * 12, "j": "abc"})
    assert with_token(forged).get("/api/documents").status_code == 401
