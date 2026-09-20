"""Startup warnings about plain HTTP and no password (M5). They only log."""

import logging

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.WARNING, logger="vibehealth")
    return caplog


@pytest.mark.parametrize("argv,expected", [
    (["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"], "0.0.0.0"),
    (["uvicorn", "app.main:app", "--host=127.0.0.1"], "127.0.0.1"),
    (["uvicorn", "app.main:app"], None),
    (["uvicorn", "--host"], None),
])
def test_bind_host_from_the_command_line(argv, expected, monkeypatch):
    monkeypatch.delenv("VIBEHEALTH_HOST", raising=False)
    assert security.bind_host(argv) == expected


def test_bind_host_from_the_environment(monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_HOST", "::1")
    assert security.bind_host(["uvicorn"]) == "::1"


@pytest.mark.parametrize("host,reachable", [
    ("127.0.0.1", False), ("localhost", False), ("::1", False), ("[::1]", False), ("LOCALHOST", False),
    ("0.0.0.0", True), ("192.0.2.100", True), ("::", True), (None, True),
])
def test_reachable_beyond_loopback(host, reachable):
    assert security.reachable_beyond_loopback(host) is reachable


def test_open_and_plain_http_warns_twice(monkeypatch, warnings):
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    messages = security.log_exposure_warnings()
    assert len(messages) == 2
    assert "No password is set" in messages[0] and "0.0.0.0" in messages[0] and "setup code" in messages[0]
    assert "plain HTTP" in messages[1] and "VIBEHEALTH_TRUST_PROXY" in messages[1]
    assert "No password is set" in warnings.text and "plain HTTP" in warnings.text


def test_a_password_without_https_still_warns_about_http(monkeypatch, warnings):
    from app import settings_store

    settings_store.update("auth", {"password_hash": security.hash_password("a long enough one")})
    monkeypatch.setattr(security, "bind_host", lambda argv=None: None)
    messages = security.log_exposure_warnings()
    assert len(messages) == 1 and "plain HTTP" in messages[0]


def test_a_password_behind_a_trusted_proxy_is_quiet(monkeypatch, warnings):
    from app import settings_store

    settings_store.update("auth", {"password_hash": security.hash_password("a long enough one")})
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    assert security.log_exposure_warnings() == []
    assert "plain HTTP" not in warnings.text


def test_loopback_only_is_quiet_even_without_a_password(monkeypatch, warnings):
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "127.0.0.1")
    assert security.log_exposure_warnings() == []


def test_no_password_behind_a_trusted_proxy_still_says_open(monkeypatch, warnings):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    messages = security.log_exposure_warnings()
    assert len(messages) == 1 and "No password is set" in messages[0]


def test_startup_logs_the_warnings_and_changes_nothing_else(monkeypatch, warnings):
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True}
        assert c.get("/api/documents").status_code == 200  # still open: a warning, not a behaviour change
    assert "No password is set" in warnings.text


@pytest.mark.parametrize("published,quiet", [
    ("127.0.0.1", True), ("127.5.6.7", True), ("::1", True), ("[::1]", True), ("localhost", True), ("LOCALHOST", True),
    ("0.0.0.0", False), ("192.0.2.10", False), ("::", False), ("example.org", False), ("", False),
])
def test_published_on_a_loopback_address_is_quiet(monkeypatch, warnings, published, quiet):
    monkeypatch.setenv("VIBEHEALTH_PUBLISHED_ON", published)
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    assert (security.log_exposure_warnings() == []) is quiet
    if not quiet:
        assert len(security.log_exposure_warnings()) == 2


def test_the_old_behind_loopback_variable_no_longer_silences_anything(monkeypatch, warnings):
    monkeypatch.setenv("VIBEHEALTH_BEHIND_LOOPBACK", "1")
    monkeypatch.delenv("VIBEHEALTH_PUBLISHED_ON", raising=False)
    monkeypatch.setattr(security, "bind_host", lambda argv=None: "0.0.0.0")
    assert len(security.log_exposure_warnings()) == 2
