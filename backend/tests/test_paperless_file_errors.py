"""The thumbnail and preview of a Paperless document when Paperless is unreachable or errors:
a clean 502 / 404 with a fixed message, never a 500 and never the exception, an address or a token."""

import httpx
import pytest
from conftest import PasswordClient
from fake_paperless import BASE, TOKEN, FakePaperless
from sqlmodel import Session

from app.db import engine
from app.main import app
from app.models import Document

client = PasswordClient(app)


@pytest.fixture
def doc_id():
    client.get("/api/status")
    assert client.put("/api/settings/paperless", json={"url": BASE, "token": TOKEN}).status_code == 200
    with Session(engine) as s:
        doc = Document(paperless_id=7, title="synthetic")
        s.add(doc)
        s.commit()
        return doc.id


def leaks(text: str) -> bool:
    return any(x in text for x in (BASE, "paperless.test", TOKEN, "Error", "Traceback", "httpx"))


@pytest.mark.parametrize("route", ["thumbnail", "preview"])
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("connection refused to http://paperless.test:8000 with Token " + TOKEN),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("slow"),
    httpx.RemoteProtocolError("bad reply"),
    RuntimeError("something unexpected: " + TOKEN),
])
def test_an_unreachable_paperless_is_a_502_with_a_fixed_message(monkeypatch, doc_id, route, failure):
    fake = FakePaperless().install(monkeypatch)
    fake.fail = failure
    r = client.get(f"/api/documents/{doc_id}/{route}")
    assert r.status_code == 502, r.text
    assert r.json() == {"detail": "Paperless could not provide this file right now."}
    assert not leaks(r.text)


@pytest.mark.parametrize("route", ["thumbnail", "preview"])
@pytest.mark.parametrize("status,expected", [(404, 404), (401, 502), (403, 502), (500, 502), (503, 502)])
def test_an_error_status_from_paperless_is_a_clean_404_or_502(monkeypatch, doc_id, route, status, expected):
    fake = FakePaperless().install(monkeypatch)
    fake.status_override = status
    r = client.get(f"/api/documents/{doc_id}/{route}")
    assert r.status_code == expected, r.text
    assert set(r.json()) == {"detail"} and not leaks(r.text)
    assert r.headers["cache-control"] == "private, no-store"


def test_a_paperless_that_is_not_configured_is_a_502_too(doc_id):
    assert client.put("/api/settings/paperless", json={"token": ""}).status_code == 200  # (no token: no fetch)
    from app import settings_store

    settings_store.update("paperless", {"url": None})
    r = client.get(f"/api/documents/{doc_id}/thumbnail")
    assert r.status_code == 502 and not leaks(r.text)


def test_the_successful_path_is_unchanged(monkeypatch, doc_id):
    fake = FakePaperless().install(monkeypatch)
    fake.body_override = httpx.Response(200, content=b"img", headers={"content-type": "image/webp"})
    t = client.get(f"/api/documents/{doc_id}/thumbnail")
    assert t.status_code == 200 and t.content == b"img" and t.headers["content-type"] == "image/webp"
    assert t.headers["cache-control"] == "private, no-store" and t.headers["x-content-type-options"] == "nosniff"
    fake.body_override = httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})
    p = client.get(f"/api/documents/{doc_id}/preview")
    assert p.status_code == 200 and p.content == b"%PDF-1.4" and p.headers["cache-control"] == "private, no-store"
    assert [r.url.path for r in fake.requests] == ["/api/documents/7/thumb/", "/api/documents/7/preview/"]


@pytest.mark.parametrize("route", ["thumbnail", "preview"])
@pytest.mark.parametrize("media_type", ["text/html", "image/svg+xml", "application/javascript", "text/plain", ""])
def test_a_media_type_outside_the_allowlist_is_a_502(monkeypatch, doc_id, route, media_type):
    """A compromised Paperless must not be able to serve HTML or SVG from the app's origin."""
    fake = FakePaperless().install(monkeypatch)
    fake.body_override = httpx.Response(200, content=b"<script>alert(1)</script>", headers={"content-type": media_type})
    r = client.get(f"/api/documents/{doc_id}/{route}")
    assert r.status_code == 502 and "script" not in r.text
    assert r.headers["cache-control"] == "private, no-store"


def test_allowed_types_are_served_with_their_own_type_and_parameters_dropped(monkeypatch, doc_id):
    fake = FakePaperless().install(monkeypatch)
    fake.body_override = httpx.Response(200, content=b"img", headers={"content-type": "Image/PNG; charset=binary"})
    r = client.get(f"/api/documents/{doc_id}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
