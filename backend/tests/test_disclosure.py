"""Errors that reach the UI (status, worker fields, helper endpoints) are short classified messages:
no internal address, no path, no text of a library exception."""

import asyncio

import httpx
import pytest
from fake_paperless import BASE, TOKEN, FakePaperless
from fastapi.testclient import TestClient

from app import reading, security, settings_store, worker
from app.main import app
from app.ollama import OllamaError
from app.paperless import classify

INTERNAL = ["192.0.2.7", "198.51.100.5", "/data/vibehealth.db", "secret-host.internal", "paperless.test"]


def has_internal(text: str) -> bool:
    return any(x in text for x in INTERNAL)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def connected(monkeypatch):
    settings_store.update("paperless", {"url": BASE, "token": TOKEN})
    monkeypatch.setitem(worker.state, "last_error", None)
    return FakePaperless().standard().install(monkeypatch)


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("[Errno 111] Connection refused to http://192.0.2.7:8000/api/"),
    httpx.ConnectTimeout("timed out connecting to 198.51.100.5"),
    httpx.ReadTimeout("read timeout http://secret-host.internal"),
    httpx.UnsupportedProtocol("Request URL has an unsupported protocol 'ftp://192.0.2.7'"),
    httpx.InvalidURL("Invalid URL http://192.0.2.7:99999"),
    httpx.HTTPStatusError("500 at http://192.0.2.7", request=httpx.Request("GET", "http://192.0.2.7"),
                          response=httpx.Response(500, request=httpx.Request("GET", "http://192.0.2.7"))),
    httpx.HTTPStatusError("401", request=httpx.Request("GET", "http://192.0.2.7"),
                          response=httpx.Response(401, request=httpx.Request("GET", "http://192.0.2.7"))),
    RuntimeError("opened /data/vibehealth.db at http://192.0.2.7"),
    ValueError("http://198.51.100.5"),
])
def test_classify_gives_fixed_messages(exc):
    kind, message = classify(exc, "http://192.0.2.7:8000")
    assert not has_internal(message) and "192.0.2.7" not in message and "198.51.100" not in message
    assert kind in ("connection", "unauthorized", "not_found", "tls", "timeout", "other")


def test_a_failed_sync_shows_a_classified_message_in_status(connected, client):
    connected.fail = httpx.ConnectError("refused: http://192.0.2.7:8000/api/tags/ [Errno 111]")
    asyncio.run(worker.run_once())
    error = worker.state["last_error"]
    assert error and not has_internal(error) and TOKEN not in error
    body = client.get("/api/status")
    assert body.status_code == 200
    data = body.json()
    assert data["worker"]["last_error"] == error
    assert not has_internal(str(data["worker"]))


def test_an_unexpected_sync_error_shows_only_its_class(connected, client, monkeypatch):
    async def boom():
        raise RuntimeError("could not write /data/vibehealth.db via http://192.0.2.7/x " + TOKEN)

    monkeypatch.setattr(worker, "sync_documents", boom)
    asyncio.run(worker.run_once())
    assert worker.state["last_error"] == "Unexpected error (RuntimeError)"
    assert "192.0.2.7" not in client.get("/api/status").text.replace(BASE, "")


def test_reading_errors_from_unexpected_failures_show_only_the_class(monkeypatch):
    async def boom(document_id, progress, force_lab=False):
        raise OSError("cannot read /data/secret.pdf from http://192.0.2.7:8000/api/documents/4/download/")

    monkeypatch.setattr(worker, "read_document", boom)
    asyncio.run(worker.read_one(4))
    r = worker.state["reading"]
    assert r["last_error"] == "Unexpected error (OSError)"
    assert r["last_run"]["error"] == "Unexpected error (OSError)"
    assert not has_internal(str(r))


def test_our_own_reading_messages_still_come_through(monkeypatch):
    async def boom(document_id, progress, force_lab=False):
        raise OllamaError("model glm-ocr is not installed in Ollama")

    monkeypatch.setattr(worker, "read_document", boom)
    asyncio.run(worker.read_one(4))
    assert worker.state["reading"]["last_error"] == "model glm-ocr is not installed in Ollama"


def test_a_reading_run_that_fails_stores_a_safe_error(monkeypatch):
    """The message saved with the run (shown per document) is the same classified text."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Document, ExtractionRun

    class Boom:
        async def download(self, *a, **kw):
            raise httpx.ConnectError("refused http://192.0.2.7:8000/api/documents/1/download/")

    monkeypatch.setattr(reading, "Paperless", Boom)
    with Session(engine) as s:
        doc = Document(paperless_id=61, title="synthetic")
        s.add(doc)
        s.commit()
        doc_id = doc.id
    with pytest.raises(OllamaError):  # OLLAMA_URL of the tests points at a closed port
        asyncio.run(reading.read_document(doc_id, {}))
    with Session(engine) as s:
        run = s.exec(select(ExtractionRun)).one()
    assert run.error == "Ollama is not reachable at the configured address."


def test_status_needs_a_session_in_password_mode_and_carries_the_address_only_then(connected, client):
    assert client.get("/api/status").json()["paperless_url"] == BASE  # open mode, as before
    owner = TestClient(app)
    assert owner.post("/api/auth/change-password",
                      json={"new": "correct horse battery", "setup_code": security.current_setup_code()}).status_code == 200
    anonymous = TestClient(app)
    r = anonymous.get("/api/status")
    assert r.status_code == 401 and BASE not in r.text
    assert owner.get("/api/status").json()["paperless_url"] == BASE


def test_paperless_helper_failures_are_fixed_strings(connected, client):
    connected.fail = httpx.ConnectError("refused http://192.0.2.7:8000/api/documents/")
    for path in ("test", "discover", "preview-count"):
        r = client.post(f"/api/paperless/{path}", json={})
        assert not has_internal(r.text), path
    connected.fail = httpx.ReadTimeout("slow http://secret-host.internal")
    for path in ("test", "discover", "preview-count"):
        assert not has_internal(client.post(f"/api/paperless/{path}", json={}).text), path
