"""Tests run against a throw-away database; nothing touches the network."""

import os
import shutil
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="vibehealth-test-")
_frontend = os.path.join(_tmp, "dist")
os.makedirs(os.path.join(_frontend, "assets"))
with open(os.path.join(_frontend, "index.html"), "w") as f:
    f.write("<!doctype html><title>test</title>")

os.environ.update(
    DATA_DIR=os.path.join(_tmp, "data"),
    VIBEHEALTH_ENV_FILE="",
    VIBEHEALTH_FRONTEND=_frontend,
    APP_PASSWORD_HASH="",
    OLLAMA_URL="http://127.0.0.1:9",  # nothing listens there
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(request):
    from sqlmodel import SQLModel

    from app import security, settings_store
    from app.throttle import login_throttle, paperless_limiter, test_limiter
    from app.worker import check_state, state

    SQLModel.metadata.drop_all(engine)
    # uploaded originals and thumbnails belong to the rows that were just dropped
    for name in ("uploads", "cache"):
        shutil.rmtree(os.path.join(_tmp, "data", name), ignore_errors=True)
    state["reading"].update(current=None, queue=[])
    check_state.update(current=None, last=None)
    init_db()
    settings_store.clear_cache()
    login_throttle.reset()
    paperless_limiter.reset()
    test_limiter.reset()
    security.clear_setup_code()
    if not request.node.get_closest_marker("fresh_install"):
        # An install without a password refuses its data routes until one is set (setup_state.py). Most
        # tests are about something else: they run in explicit legacy-open mode, as an operator who
        # chose VIBEHEALTH_LEGACY_OPEN=1 would. Tests of the first-run behaviour mark themselves
        # `fresh_install`, which leaves the variable unset.
        os.environ["VIBEHEALTH_LEGACY_OPEN"] = "1"
    else:
        os.environ.pop("VIBEHEALTH_LEGACY_OPEN", None)
    yield
    os.environ.pop("VIBEHEALTH_LEGACY_OPEN", None)
    settings_store.clear_cache()


@pytest.fixture(autouse=True)
def cheap_scrypt(request, monkeypatch):
    """Hashing with the real settings takes 0.2 s: the many tests that only need *a* password use a
    cheap profile. Tests of the real one mark themselves `real_scrypt`."""
    if request.node.get_closest_marker("real_scrypt"):
        return
    from app import security

    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)
    monkeypatch.setattr(security, "_SCRYPT_P", 1)


@pytest.fixture
def reload_config(monkeypatch):
    """Change environment variables for one test: call the result after monkeypatch.setenv."""
    from app import secret_store, settings_store
    from app.config import get_settings

    def reload():
        get_settings.cache_clear()
        secret_store.reset_cache()
        settings_store.clear_cache()

    reload()
    yield reload
    monkeypatch.undo()
    reload()


# --- a client that is signed in with a password ------------------------------------------------
# Changing a Paperless / Ollama address or the trust lists needs a password (see
# settings_store.SENSITIVE). Tests of the settings themselves use this client: it sets a password
# lazily (every test starts with an empty database), signs in, and adds `current_password` to the
# PUTs that need it. The tests of the rule itself use plain clients.

TEST_PASSWORD = "synthetic test password"
_TEST_HASH: list[str] = []


class PasswordClient(TestClient):
    _busy = False

    def _ensure(self):
        from app import security, settings_store

        if self._busy or security.password_hash():
            return
        if not _TEST_HASH:
            _TEST_HASH.append(security.hash_password(TEST_PASSWORD))
        settings_store.update("auth", {"password_hash": _TEST_HASH[0], "session_epoch": 1})
        self._busy = True
        try:
            self.cookies.clear()
            r = super().request("POST", "/api/auth/login", json={"password": TEST_PASSWORD})
            assert r.status_code == 200, r.text
        finally:
            self._busy = False

    def relogin(self):
        """After the signing key changed (SECRET_KEY) or the session was ended."""
        self._busy = True
        try:
            self.cookies.clear()
            r = super().request("POST", "/api/auth/login", json={"password": TEST_PASSWORD})
            assert r.status_code == 200, r.text
        finally:
            self._busy = False

    def request(self, method, url, **kw):
        if not self._busy:
            self._ensure()
            body = kw.get("json")
            if method.upper() == "PUT" and isinstance(body, dict) and str(url).startswith(
                ("/api/settings/", "/api/reading/settings")
            ):
                kw["json"] = {**body, "current_password": TEST_PASSWORD}
            elif method.upper() == "POST" and isinstance(body, dict) and str(url).startswith(
                ("/api/paperless/test", "/api/reading/test-connection")
            ) and (body.get("url") or body.get("ollama_url")):
                kw["json"] = {**body, "current_password": TEST_PASSWORD}  # trying another address
        return super().request(method, url, **kw)
