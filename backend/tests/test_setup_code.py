"""The first password needs the one-time setup code; `reset-password` is the way back in."""

import logging
import os
import stat
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from app import security, settings_store
from app.main import app

PASSWORD = "correct horse battery"
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def new_client() -> TestClient:
    return TestClient(app)


def claim(client, code, password=PASSWORD):
    return client.post("/api/auth/change-password", json={"new": password, "setup_code": code})


@pytest.fixture
def client():
    return new_client()


def test_first_password_without_or_with_a_wrong_code_is_refused(client):
    security.current_setup_code()
    assert client.post("/api/auth/change-password", json={"new": PASSWORD}).status_code == 403
    assert claim(client, "").status_code == 403
    assert claim(client, "WRONG123").status_code == 403
    # still open, nothing was set
    assert client.get("/api/auth/status").json()["mode"] == "open_legacy"
    assert client.get("/api/documents").status_code == 200
    assert not security.password_hash()


def test_the_right_code_sets_the_password_and_uses_the_code_up(client):
    code = security.current_setup_code()
    assert os.path.exists(security.setup_code_path())
    r = claim(client, code)
    assert r.status_code == 200 and security.COOKIE_NAME in r.headers["set-cookie"]
    assert security.password_hash()
    assert not os.path.exists(security.setup_code_path())  # invalidated
    other = new_client()
    assert other.get("/api/documents").status_code == 401
    # the code is worthless afterwards, as a `current` password too
    assert other.post("/api/auth/change-password", json={"new": "x" * 12, "setup_code": code}).status_code == 401
    assert client.post("/api/auth/change-password", json={"new": "y" * 12, "current": code}).status_code == 403


def test_code_is_case_and_space_insensitive(client):
    code = security.current_setup_code()
    spaced = f" {code[:4].lower()} {code[4:].lower()} "
    assert claim(client, spaced).status_code == 200


def test_code_shape_and_file():
    code = security.current_setup_code()
    assert len(code) == 8 and set(code) <= set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    assert not set(code) & set("0O1I")
    with open(security.setup_code_path()) as f:
        assert f.read().strip() == code
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(security.setup_code_path()).st_mode) == 0o600


def test_codes_are_random():
    codes = {security.new_setup_code(announce=False) for _ in range(20)}
    assert len(codes) > 15


def test_a_new_code_replaces_the_old_one(client):
    old = security.current_setup_code()
    new = old
    while new == old:
        new = security.new_setup_code(announce=False)
    assert claim(client, old).status_code == 403
    assert claim(client, new).status_code == 200


def test_wrong_codes_count_against_the_login_throttle(client):
    real = security.current_setup_code()
    for _ in range(5):
        assert claim(client, "BADCODE2").status_code == 403
    r = claim(client, real)  # even the right code has to wait now
    assert r.status_code == 429 and "retry-after" in r.headers
    assert not security.password_hash()


def test_startup_makes_and_logs_a_code_while_there_is_no_password(caplog):
    caplog.set_level(logging.INFO, logger="vibehealth")
    security.clear_setup_code()
    security.ensure_setup_code()
    code = security.current_setup_code()
    assert f"Setup code: {code}" in caplog.text
    assert security.setup_code_path() in caplog.text
    security.ensure_setup_code()  # every start makes a new one
    assert os.path.exists(security.setup_code_path())


def test_startup_with_a_password_leaves_no_code_file(client):
    assert claim(client, security.current_setup_code()).status_code == 200
    with open(security.setup_code_path(), "w") as f:  # a leftover
        f.write("LEFTOVER\n")
    security.ensure_setup_code()
    assert not os.path.exists(security.setup_code_path())


def test_lifespan_startup_writes_the_code_file(caplog):
    caplog.set_level(logging.INFO, logger="vibehealth")
    security.clear_setup_code()
    with TestClient(app):
        assert os.path.exists(security.setup_code_path())
    assert "Setup code:" in caplog.text


def test_status_says_whether_the_setup_code_is_needed(client):
    assert client.get("/api/auth/status").json()["setup_code_required"] is True
    assert claim(client, security.current_setup_code()).status_code == 200
    assert client.get("/api/auth/status").json()["setup_code_required"] is False


def test_the_code_is_never_in_an_error_body(client):
    code = security.current_setup_code()
    r = claim(client, "WRONG123")
    assert code not in r.text


def test_comparison_handles_odd_input():
    assert security.check_setup_code(security.current_setup_code()) is True
    assert security.check_setup_code("") is False
    assert security.check_setup_code("é" * 8) is False  # non-ascii input must not crash


def test_concurrent_claims_have_one_winner():
    code = security.current_setup_code()
    results = []

    def go(n):
        results.append(claim(new_client(), code, password=f"password-number-{n}").status_code)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1
    assert all(r in (401, 403, 429) for r in results if r != 200)


# --- reset-password (server access) ---------------------------------------------------


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "app.security", *args],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
    )


def test_reset_password_cli_closes_the_app_and_makes_a_new_code(client, monkeypatch):
    assert claim(client, security.current_setup_code()).status_code == 200
    assert new_client().get("/api/documents").status_code == 401
    old_epoch = security.session_epoch()

    done = run_cli("reset-password")
    assert done.returncode == 0, done.stderr
    settings_store.clear_cache()
    assert not security.password_hash()
    assert security.session_epoch() > old_epoch
    monkeypatch.delenv("VIBEHEALTH_LEGACY_OPEN")
    r = new_client().get("/api/documents")
    assert r.status_code == 403 and r.json() == {"detail": "setup_required"}  # gated until a new password is set
    assert os.path.exists(security.setup_code_path())
    code = security.current_setup_code()
    assert security.setup_code_path() in done.stdout
    assert code not in done.stdout and code not in done.stderr
    # the owner sets a new password with the new code
    assert claim(new_client(), code, password="brand new password").status_code == 200


def test_reset_password_cli_rejects_unknown_arguments():
    assert run_cli("nonsense").returncode != 0


def test_reset_password_keeps_an_env_password_and_says_so(monkeypatch, reload_config, capsys):
    monkeypatch.setenv("APP_PASSWORD_HASH", security.hash_password(PASSWORD))
    reload_config()
    security.reset_password()
    out = capsys.readouterr().out
    assert "APP_PASSWORD_HASH" in out
    assert security.password_hash()  # the env hash still applies
    assert not os.path.exists(security.setup_code_path())


def test_the_cli_still_makes_a_hash(monkeypatch, capsys):
    answers = iter(["a long enough one", "a long enough one"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    security._main([])
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("APP_PASSWORD_HASH=")][0]
    assert security.verify_password("a long enough one", line.split("=", 1)[1])
