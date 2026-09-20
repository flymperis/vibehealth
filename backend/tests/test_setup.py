"""First-run setup: the derived state, the fresh-install gate, readiness and Ollama detection."""

import asyncio
import json

import httpx
import pytest
from conftest import PasswordClient
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import ollama, security, settings_store
from app.db import engine
from app.main import app
from app.models import Document
from app.throttle import detect_limiter

fresh = pytest.mark.fresh_install
REAL_CLIENT = httpx.AsyncClient


def use_transport(monkeypatch, handler):
    monkeypatch.setattr(ollama.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def status(client):
    return client.get("/api/setup/status").json()


def add_document():
    with Session(engine) as session:
        session.add(Document(title="x"))
        session.commit()


@pytest.fixture(autouse=True)
def _reset():
    detect_limiter.reset()


# --- the derived state ------------------------------------------------------------------


@fresh
def test_a_fresh_install_needs_setup():
    s = status(TestClient(app))
    # public, so only what the guide and the login page need: nothing about sources or documents
    assert s == {"state": "needs_setup", "password_set": False, "needs_setup_code": True, "wizard_pending": True}
    assert TestClient(app).get("/api/auth/status").json()["mode"] == "needs_setup"


@fresh
def test_documents_alone_do_not_open_the_api():
    add_document()
    c = TestClient(app)
    assert status(c)["state"] == "needs_setup"
    assert c.get("/api/documents").status_code == 403 and c.get("/api/documents").json() == {"detail": "setup_required"}
    assert c.get("/api/dashboard/summary").status_code == 403


@fresh
def test_a_token_from_the_environment_alone_does_not_open_the_api(monkeypatch, reload_config):
    monkeypatch.setenv("PAPERLESS_TOKEN", "synthetic-env-token")
    reload_config()
    c = TestClient(app)
    assert status(c)["state"] == "needs_setup"
    assert c.get("/api/documents").status_code == 403


@fresh
def test_a_token_saved_in_the_app_alone_does_not_open_the_api():
    settings_store.update("paperless", {"token": "synthetic-app-token"})
    c = TestClient(app)
    assert status(c)["state"] == "needs_setup"
    assert c.get("/api/documents").status_code == 403


@fresh
def test_the_completed_flag_alone_does_not_open_the_api():
    settings_store.update("setup", {"completed": True})
    c = TestClient(app)
    assert status(c)["state"] == "needs_setup"
    assert c.get("/api/documents").status_code == 403


@fresh
def test_a_password_makes_it_ready_even_with_nothing_else():
    settings_store.update("auth", {"password_hash": security.hash_password("x" * 12)})
    s = status(TestClient(app))
    assert s["state"] == "ready" and s["password_set"] is True and s["needs_setup_code"] is False
    assert s["wizard_pending"] is True  # a password but the guide never finished: it opens by itself
    settings_store.update("setup", {"completed": True})
    assert status(TestClient(app))["wizard_pending"] is False


@fresh
def test_a_password_in_the_environment_opens_it_too(monkeypatch, reload_config):
    monkeypatch.setenv("APP_PASSWORD_HASH", security.hash_password("x" * 12))
    reload_config()
    c = TestClient(app)
    assert status(c)["state"] == "ready"
    assert c.get("/api/documents").status_code == 401  # ready, and protected


@fresh
def test_the_legacy_variable_opens_a_password_less_install(monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_LEGACY_OPEN", "1")
    c = TestClient(app)
    s = status(c)
    assert s["state"] == "ready" and s["password_set"] is False and s["wizard_pending"] is False
    assert c.get("/api/auth/status").json()["mode"] == "open_legacy"
    assert c.get("/api/documents").status_code == 200
    monkeypatch.setenv("VIBEHEALTH_LEGACY_OPEN", "0")
    assert c.get("/api/documents").status_code == 403


@fresh
def test_a_password_wins_over_the_legacy_variable(monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_LEGACY_OPEN", "1")
    settings_store.update("auth", {"password_hash": security.hash_password("x" * 12)})
    c = TestClient(app)
    assert c.get("/api/auth/status").json()["mode"] == "password"
    assert c.get("/api/documents").status_code == 401


@fresh
def test_reset_password_closes_the_install_again(monkeypatch):
    settings_store.update("auth", {"password_hash": security.hash_password("x" * 12)})
    add_document()
    c = TestClient(app)
    assert c.get("/api/documents").status_code == 401  # ready, needs a session
    security.reset_password()
    assert not security.password_hash()
    r = c.get("/api/documents")
    assert r.status_code == 403 and r.json() == {"detail": "setup_required"}  # closed, not open
    s = status(c)
    assert s["state"] == "needs_setup" and s["wizard_pending"] is True


# --- the gate ---------------------------------------------------------------------------


@fresh
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/documents"), ("GET", "/api/status"), ("GET", "/api/settings"),
    ("GET", "/api/reading/settings"), ("GET", "/api/reading/readiness"), ("POST", "/api/reading/detect-ollama"),
    ("POST", "/api/auth/login"), ("POST", "/api/sync"), ("GET", "/api/nothing-here"),
])
def test_data_routes_answer_setup_required_on_a_fresh_install(method, path):
    r = TestClient(app).request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 403 and r.json() == {"detail": "setup_required"}


@fresh
def test_the_setup_routes_stay_open_on_a_fresh_install():
    c = TestClient(app)
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/auth/status").status_code == 200
    assert c.get("/api/setup/status").status_code == 200
    assert c.get("/").status_code == 200  # the app shell
    # the first password is not gated, and it is checked against the code as usual
    assert c.post("/api/auth/change-password", json={"new": "x" * 12, "setup_code": "WRONG123"}).status_code == 403


@fresh
def test_the_first_password_opens_the_session_and_the_gate():
    c = TestClient(app)
    code = security.current_setup_code()
    r = c.post("/api/auth/change-password", json={"new": "correct horse", "setup_code": code})
    assert r.status_code == 200
    assert status(c)["state"] == "ready"
    assert c.get("/api/documents").status_code == 200  # this browser is signed in
    assert TestClient(app).get("/api/documents").status_code == 401  # everybody else needs the password


@fresh
def test_complete_is_refused_without_a_password_and_then_sets_the_flag(monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_LEGACY_OPEN", "1")  # ready, but still no password
    assert not security.password_hash()
    r = TestClient(app).post("/api/setup/complete")
    assert r.status_code == 409 and not settings_store.value("setup", "completed")
    pc = PasswordClient(app)
    assert pc.post("/api/setup/complete").json() == {"ok": True}
    assert settings_store.value("setup", "completed") is True
    assert status(pc)["wizard_pending"] is False


@fresh
def test_complete_needs_a_session():
    settings_store.update("auth", {"password_hash": security.hash_password("x" * 12)})
    assert TestClient(app).post("/api/setup/complete").status_code == 401


def test_a_legacy_install_stays_open_and_never_needs_setup():
    """VIBEHEALTH_LEGACY_OPEN=1 (the default of the other tests), no password, the guide never completed."""
    add_document()
    c = TestClient(app)
    s = status(c)
    assert s["state"] == "ready" and s["wizard_pending"] is False
    a = c.get("/api/auth/status").json()
    assert a["mode"] == "open_legacy" and a["authenticated"] is True
    assert c.get("/api/documents").status_code == 200
    assert c.get("/api/status").status_code == 200


# --- readiness --------------------------------------------------------------------------


def ollama_with(monkeypatch, models, version="0.12.3"):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": version})
        return httpx.Response(404)

    use_transport(monkeypatch, handler)


def test_readiness_shape_all_installed(monkeypatch):
    ollama_with(monkeypatch, ["qwen3.5:4b", "glm-ocr:latest"])
    r = PasswordClient(app).get("/api/reading/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["ollama"] == {"reachable": True, "version": "0.12.3", "url": "http://127.0.0.1:9"}
    assert body["models"] == [
        {"role": "reader_a", "name": "qwen3.5:4b", "installed": True, "pull_command": "ollama pull qwen3.5:4b"},
        {"role": "reader_b", "name": "glm-ocr", "installed": True, "pull_command": "ollama pull glm-ocr"},
    ]
    assert body["ready"] is True and body["missing"] == []


def test_readiness_lists_what_to_pull(monkeypatch):
    ollama_with(monkeypatch, ["qwen3.5:4b"])
    body = PasswordClient(app).get("/api/reading/readiness").json()
    assert body["ready"] is False and body["missing"] == ["ollama pull glm-ocr"]
    assert [m["installed"] for m in body["models"]] == [True, False]


def test_reader_b_is_not_required_while_it_is_off(monkeypatch):
    ollama_with(monkeypatch, ["qwen3.5:4b"])
    settings_store.update("reading", {"reader_b_enabled": False})
    body = PasswordClient(app).get("/api/reading/readiness").json()
    assert [m["role"] for m in body["models"]] == ["reader_a"]
    assert body["ready"] is True and body["missing"] == []


def test_readiness_when_ollama_is_down_gives_no_details(monkeypatch):
    def down(request):
        raise httpx.ConnectError("boom http://secret.internal:1")

    use_transport(monkeypatch, down)
    r = PasswordClient(app).get("/api/reading/readiness")
    body = r.json()
    assert body["ollama"]["reachable"] is False and body["ollama"]["version"] == ""
    assert body["ready"] is False and len(body["missing"]) == 2
    assert "secret.internal" not in r.text and "boom" not in r.text


def test_readiness_needs_a_session():
    PasswordClient(app).get("/api/documents")  # sets a password
    assert TestClient(app).get("/api/reading/readiness").status_code == 401


# --- detection --------------------------------------------------------------------------


def test_detect_probes_only_the_fixed_list_and_takes_no_url(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host in ("localhost", "172.17.0.1"):
            return httpx.Response(200, json={"version": "0.9.0"})
        raise httpx.ConnectError("no")

    use_transport(monkeypatch, handler)
    c = PasswordClient(app)
    # whatever the client sends, no address of its own is ever probed
    r = c.post("/api/reading/detect-ollama", json={"url": "http://evil.example:80", "ollama_url": "http://evil.example"})
    assert r.status_code == 200
    assert r.json() == {"found": [
        {"url": "http://localhost:11434", "version": "0.9.0"},
        {"url": "http://172.17.0.1:11434", "version": "0.9.0"},
    ]}
    assert sorted(seen) == sorted(f"{u}/api/version" for u in ollama.DETECT_CANDIDATES)
    assert not any("evil" in u for u in seen)
    assert ollama.DETECT_CANDIDATES == (
        "http://host.docker.internal:11434", "http://localhost:11434",
        "http://127.0.0.1:11434", "http://172.17.0.1:11434",
    )


def test_detect_ignores_answers_that_are_not_ollama(monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(200, content=b"<html>router login</html>"))
    assert asyncio.run(ollama.detect()) == []


def test_detect_is_rate_limited(monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(404))
    c = PasswordClient(app)
    codes = [c.post("/api/reading/detect-ollama").status_code for _ in range(8)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
