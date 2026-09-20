"""Login, throttling, sessions, cookie flags, Origin check, hashes, and the open (no password) mode."""

import base64
import hashlib
import logging

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app import security, settings_store
from app.main import app
from app.throttle import Throttle, login_throttle

PASSWORD = "correct horse battery"
OTHER = "another synthetic pass"


def new_client(**kw) -> TestClient:
    return TestClient(app, **kw)


@pytest.fixture
def client():
    return new_client()


@pytest.fixture
def protected(client):
    """An instance with a password set, and a client that is signed in."""
    assert first_password(client).status_code == 200
    return client


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]

    def tick():
        return now[0]

    for t in (login_throttle.per_ip, login_throttle.everyone):
        monkeypatch.setattr(t, "clock", tick)
    return now


def first_password(client, password=PASSWORD, code=None, **kw):
    """Set the first password the way the owner does: with the setup code from the server."""
    body = {"new": password, "setup_code": security.current_setup_code() if code is None else code}
    return client.post("/api/auth/change-password", json=body, **kw)


def login(client, password=PASSWORD, **kw):
    return client.post("/api/auth/login", json={"password": password}, **kw)


# --- open_legacy: an instance without a password behaves exactly as before ------------


def test_no_password_instance_stays_open(client):
    status = client.get("/api/auth/status").json()
    assert status["password_required"] is False and status["authenticated"] is True
    assert status["password_set"] is False and status["mode"] == "open_legacy"
    assert status["default_language"] == status["language"] == "en"
    assert client.get("/api/documents").status_code == 200
    assert client.get("/api/reading/settings").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.post("/api/auth/login", json={"password": "anything"}).json() == {
        "ok": True, "note": "no password configured"
    }
    assert client.post("/api/auth/logout").status_code == 200


def test_no_password_never_locks_anyone_out(client):
    for _ in range(40):
        r = login(client, "whatever")
        assert r.status_code == 200
    assert client.get("/api/documents").status_code == 200
    assert login_throttle.locked_for("testclient") == 0


def test_first_password_needs_the_setup_code_and_closes_the_instance(client):
    assert first_password(client).status_code == 200
    assert client.get("/api/documents").status_code == 200  # this browser stays signed in
    other = new_client()
    assert other.get("/api/documents").status_code == 401
    assert other.get("/api/settings").status_code == 401
    status = other.get("/api/auth/status").json()
    assert status["password_required"] and not status["authenticated"] and status["mode"] == "password"
    assert login(other, "wrong").status_code == 401
    assert login(other).status_code == 200
    assert other.get("/api/documents").status_code == 200
    # unknown API paths are still a plain 404 for a signed-in client
    assert other.get("/api/nope").status_code == 404


# --- hashes ----------------------------------------------------------------------------


def legacy_pbkdf2(password: str, iterations: int = 240_000) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def test_scrypt_hash_verifies():
    stored = security.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password(PASSWORD + "x", stored)
    assert security.hash_password(PASSWORD) != stored  # random salt


def test_legacy_pbkdf2_hash_still_verifies():
    stored = legacy_pbkdf2(PASSWORD)
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password("nope", stored)


@pytest.mark.parametrize("bad", ["", "x", "scrypt$1$2", "scrypt$3$8$1$AAAA$AAAA", "md5$a$b$c",
                                 "scrypt$1048576$8$1$AAAA$AAAA", "pbkdf2_sha256$x$y$z",
                                 "pbkdf2_sha256$999999999$AAAA$AAAA"])
def test_malformed_hashes_never_verify_or_crash(bad):
    assert security.verify_password(PASSWORD, bad) is False


def test_env_legacy_hash_works_until_the_app_sets_its_own(monkeypatch, reload_config):
    monkeypatch.setenv("APP_PASSWORD_HASH", legacy_pbkdf2(PASSWORD))
    reload_config()
    c = new_client()
    assert c.get("/api/auth/status").json()["password_required"] is True
    assert c.get("/api/documents").status_code == 401
    assert login(c).status_code == 200 and c.get("/api/documents").status_code == 200
    # changing it in the app: the saved hash (scrypt) now wins over the env one
    assert c.post("/api/auth/change-password", json={"current": PASSWORD, "new": OTHER}).status_code == 200
    assert login(new_client()).status_code == 401
    assert login(new_client(), OTHER).status_code == 200
    assert settings_store.resolve("auth").sources["password_hash"] == "app"


# --- change password ----------------------------------------------------------------------


def test_change_password_checks_current_and_length(protected):
    r = protected.post("/api/auth/change-password", json={"current": "wrong", "new": OTHER})
    assert r.status_code == 403
    assert protected.post("/api/auth/change-password", json={"new": OTHER}).status_code == 403
    r = protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": "short7!"})
    assert r.status_code == 422 and "short7!" not in r.text
    r = protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": "123456789"})  # 9: too short
    assert r.status_code == 422 and "123456789" not in r.text
    r = protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": "1234567890"})  # 10: enough
    assert r.status_code == 200
    assert login(new_client(), "1234567890").status_code == 200
    assert login(new_client(), PASSWORD).status_code == 401


def test_the_first_password_needs_ten_characters_too(client):
    assert security.MIN_PASSWORD_LENGTH == 10
    code = security.current_setup_code()
    r = client.post("/api/auth/change-password", json={"new": "123456789", "setup_code": code})
    assert r.status_code == 422 and not security.password_hash()
    r = client.post("/api/auth/change-password", json={"new": "1234567890", "setup_code": code})
    assert r.status_code == 200 and security.password_hash()


def test_change_password_requires_a_session(protected):
    other = new_client()
    r = other.post("/api/auth/change-password", json={"current": PASSWORD, "new": OTHER})
    assert r.status_code == 401


def test_passwords_never_reach_logs_or_errors(protected, caplog):
    with caplog.at_level(logging.DEBUG):
        texts = [
            protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": 12345}).text,
            protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": "tiny"}).text,
            new_client().post("/api/auth/login", json={"password": {"x": PASSWORD}}).text,
            new_client().post("/api/auth/login", json=[PASSWORD]).text,
        ]
    for text in texts:
        assert PASSWORD not in text and "tiny" not in text
    assert PASSWORD not in caplog.text


# --- sessions ------------------------------------------------------------------------------


def test_password_change_ends_every_other_session(protected):
    second = new_client()
    assert login(second).status_code == 200
    old_token = second.cookies.get(security.COOKIE_NAME)
    assert second.get("/api/documents").status_code == 200

    r = protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": OTHER})
    assert r.status_code == 200
    assert protected.get("/api/documents").status_code == 200  # the changing browser is re-issued
    assert second.get("/api/documents").status_code == 401
    replay = new_client()
    replay.cookies.set(security.COOKIE_NAME, old_token)
    assert replay.get("/api/documents").status_code == 401
    assert replay.get("/api/auth/status").json()["authenticated"] is False


def test_session_epoch_is_carried_in_the_token(protected):
    token = protected.cookies.get(security.COOKIE_NAME)
    data = security._serializer().loads(token)
    assert data["e"] == 1 == security.session_epoch()


def test_logout_clears_the_cookie(protected):
    r = protected.post("/api/auth/logout")
    assert security.COOKIE_NAME in r.headers["set-cookie"]
    assert protected.get("/api/documents").status_code == 401


def test_forged_and_foreign_tokens_are_refused(protected):
    forged = URLSafeTimedSerializer("dev-insecure", salt="session").dumps({"v": 1})  # the old fallback key
    other = new_client()
    other.cookies.set(security.COOKIE_NAME, forged)
    assert other.get("/api/documents").status_code == 401
    other.cookies.set(security.COOKIE_NAME, "garbage")
    assert other.get("/api/documents").status_code == 401


def test_session_key_is_derived_not_the_secret_itself(monkeypatch, reload_config):
    monkeypatch.setenv("SECRET_KEY", "synthetic-secret-key")
    reload_config()
    key = security.secret_store.session_signing_key()
    assert key != b"synthetic-secret-key" and len(key) == 32
    unsigned = URLSafeTimedSerializer("synthetic-secret-key", salt="session").dumps({"v": 1, "e": 0})
    assert security.valid_session(unsigned) is False


# --- cookie flags --------------------------------------------------------------------------------


def cookie_header(response) -> str:
    return response.headers["set-cookie"].lower()


def test_cookie_flags_over_http(protected):
    r = login(new_client())
    header = cookie_header(r)
    assert "httponly" in header and "samesite=strict" in header and "path=/" in header
    assert "secure" not in header.replace("samesite", "")


def test_cookie_is_secure_over_https():
    c = new_client(base_url="https://testserver")
    r = first_password(c)
    header = cookie_header(r)
    assert "secure" in header and "httponly" in header and "samesite=strict" in header
    assert c.get("/api/documents").status_code == 200  # the client sends a Secure cookie back over https


def test_forwarded_proto_counts_only_when_the_proxy_is_trusted(protected, monkeypatch):
    header = {"X-Forwarded-Proto": "https"}
    assert "secure" not in cookie_header(login(new_client(), headers=header)).replace("samesite", "")
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    assert "; secure" in cookie_header(login(new_client(), headers=header))
    assert "; secure" not in cookie_header(login(new_client(), headers={"X-Forwarded-Proto": "http"}))


# --- throttling ------------------------------------------------------------------------------------


def test_lockout_after_five_failures_with_retry_after(protected, clock):
    c = new_client()
    for _ in range(5):
        assert login(c, "wrong").status_code == 401
    r = login(c, "wrong")
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    # the right password does not get in while locked, and does not extend the lock
    assert login(c).status_code == 429
    clock[0] += 10
    assert login(c).headers["retry-after"] == "20"
    clock[0] += 20
    assert login(c).status_code == 200  # lock over, and a success clears the counter
    for _ in range(5):
        assert login(new_client(), "wrong").status_code == 401


def test_lockout_doubles_up_to_fifteen_minutes(protected, clock):
    c = new_client()
    for _ in range(5):
        login(c, "wrong")
    seen = []
    for _ in range(8):
        wait = int(login(c, "wrong").headers["retry-after"])
        seen.append(wait)
        clock[0] += wait  # wait it out, then fail again
        assert login(c, "wrong").status_code == 401
    assert seen[:5] == [30, 60, 120, 240, 480]
    assert max(seen) == 900
    assert int(login(c, "wrong").headers["retry-after"]) == 900


def test_lock_is_per_client_address(protected, clock, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    a, b = {"X-Forwarded-For": "198.51.100.1"}, {"X-Forwarded-For": "198.51.100.2"}
    c = new_client()
    for _ in range(5):
        assert login(c, "wrong", headers=a).status_code == 401
    assert login(c, headers=a).status_code == 429
    assert login(c, headers=b).status_code == 200


def test_global_backoff_slows_many_addresses_but_never_locks_out(protected, clock, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    c = new_client()
    for n in range(20):  # 20 failures, never more than 2 from one address
        r = login(c, "wrong", headers={"X-Forwarded-For": f"10.1.{n // 2}.{n % 2}"})
        assert r.status_code == 401
    r = login(c, headers={"X-Forwarded-For": "198.51.100.99"})  # a fresh address has to wait a little
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    clock[0] += 5
    assert login(c, headers={"X-Forwarded-For": "198.51.100.99"}).status_code == 200  # ...and then gets in


def test_a_forwarded_header_is_ignored_without_trusting_the_proxy(protected, clock):
    c = new_client()
    for n in range(5):
        assert login(c, "wrong", headers={"X-Forwarded-For": f"203.0.113.{n}"}).status_code == 401
    assert login(c, headers={"X-Forwarded-For": "203.0.113.99"}).status_code == 429  # same real address


def test_change_password_guesses_are_throttled_too(protected, clock):
    for _ in range(5):
        assert protected.post("/api/auth/change-password", json={"current": "x", "new": OTHER}).status_code == 403
    r = protected.post("/api/auth/change-password", json={"current": PASSWORD, "new": OTHER})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_throttle_forgets_idle_keys():
    now = [0.0]
    t = Throttle(2, clock=lambda: now[0])
    t.reserve("k")
    now[0] += 4000
    t.reserve("k")  # counted from zero again: still below the threshold of 2
    assert t.locked_for("k") == 0
    t.reserve("k")
    assert t.locked_for("k") == 30


# --- Origin check -----------------------------------------------------------------------------------


def post(client, origin=None, host=None, method="post"):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    return getattr(client, method)("/api/auth/logout", headers=headers)


def test_no_origin_header_is_allowed(client):
    assert post(client).status_code == 200


def test_matching_origin_is_allowed(client, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_ALLOWED_HOSTS", "health.example.com")  # a public name is not built in
    assert post(client, origin="http://testserver").status_code == 200
    assert post(client, origin="http://192.0.2.5:5001", host="192.0.2.5:5001").status_code == 200
    assert post(client, origin="https://health.example.com", host="health.example.com").status_code == 200
    assert post(client, origin="HTTP://TestServer").status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "http://testserver:8080", "null",
                                    "https://testserver.evil.example", "ftp://testserver", ""])
def test_foreign_origin_is_refused(client, origin):
    r = post(client, origin=origin)
    assert r.status_code == 403 and r.json() == {"detail": "cross-origin request refused"}


def test_every_unsafe_method_is_checked_and_reads_are_not(client):
    for method in ("post", "put", "patch", "delete"):
        assert client.request(method, "/api/settings/general", json={},
                              headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://evil.example"}).status_code == 200
    # the refusal happens before anything is changed
    r = client.put("/api/settings/general", json={"language": "el"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/settings/general").json()["values"]["language"] == "en"


def test_trusted_origins_setting_allows_a_proxy_front(protected):
    client = protected
    assert post(client, origin="https://health.example.com").status_code == 403
    r = client.put("/api/settings/general", json={"trusted_origins": ["https://health.example.com/"],
                                                   "current_password": PASSWORD})
    assert r.status_code == 200
    assert post(client, origin="https://health.example.com").status_code == 200
    assert post(client, origin="https://other.example.com").status_code == 403


def test_forwarded_host_counts_only_when_the_proxy_is_trusted(client, monkeypatch):
    def via_proxy():
        return client.post("/api/auth/logout", headers={
            "Origin": "https://health.example.com", "Host": "vibehealth:5001",
            "X-Forwarded-Host": "health.example.com"})

    assert via_proxy().status_code == 403
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    assert via_proxy().status_code == 200


def test_login_from_a_foreign_origin_is_refused(protected):
    r = login(new_client(), headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# --- language --------------------------------------------------------------------------------------------


def test_language_saved_in_the_app_beats_env(monkeypatch, reload_config, client):
    monkeypatch.setenv("DEFAULT_LANGUAGE", "el")
    reload_config()
    assert client.get("/api/auth/status").json()["language"] == "el"
    assert client.put("/api/settings/general", json={"language": "en"}).status_code == 200
    assert client.get("/api/auth/status").json()["language"] == "en"
    assert client.put("/api/settings/general", json={"language": None}).status_code == 200
    assert client.get("/api/auth/status").json()["default_language"] == "el"
