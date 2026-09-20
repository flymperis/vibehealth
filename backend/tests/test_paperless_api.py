"""POST /api/paperless/test, /discover and /preview-count against a fake Paperless."""

import logging
import ssl

import httpx
import pytest
from fake_paperless import BASE, TOKEN, FakePaperless
from conftest import PasswordClient
from fastapi.testclient import TestClient

from app import security
from app.main import app
from app.throttle import paperless_limiter

client = PasswordClient(app)
OTHER = "http://other.test:9000"


def save(**body):
    r = client.put("/api/settings/paperless", json=body)
    assert r.status_code == 200, r.text


@pytest.fixture
def fake(monkeypatch):
    return FakePaperless().standard().install(monkeypatch)


@pytest.fixture
def connected(fake):
    save(url=BASE, token=TOKEN)
    return fake


# --- gate and brake ---------------------------------------------------------------------------


def test_all_three_need_a_session(connected):
    anonymous = TestClient(app)
    for path in ("test", "discover", "preview-count"):
        assert anonymous.post(f"/api/paperless/{path}").status_code == 401
        assert anonymous.post(f"/api/paperless/{path}", json={}).status_code == 401
    assert connected.requests == []  # nothing left the server
    assert paperless_limiter.hit() == 0 and len(paperless_limiter._hits) == 1  # and no allowance was used
    assert client.post("/api/paperless/test").status_code == 200  # the signed-in client is fine


def test_calls_are_rate_limited(connected, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(paperless_limiter, "clock", lambda: now[0])
    for _ in range(paperless_limiter.limit):
        assert client.post("/api/paperless/preview-count").status_code == 200
    r = client.post("/api/paperless/test")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert client.post("/api/paperless/discover").status_code == 429
    now[0] += paperless_limiter.window + 1
    assert client.post("/api/paperless/test").status_code == 200


# --- test -------------------------------------------------------------------------------------


def test_saved_settings_are_tested_without_a_body(connected):
    r = client.post("/api/paperless/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "2.14.7", "error_kind": None, "error": None}
    assert client.post("/api/paperless/test", json={}).json()["ok"] is True
    assert connected.requests[0].headers["authorization"] == f"Token {TOKEN}"
    assert connected.requests[0].url.params["page_size"] == "1"


def test_unsaved_values_can_be_tested(fake):
    r = client.post("/api/paperless/test", json={"url": BASE + "/", "token": TOKEN})
    assert r.json()["ok"] is True
    # nothing was saved by testing
    assert client.get("/api/settings/paperless").json()["values"]["url"] == ""
    assert client.get("/api/settings/paperless").json()["values"]["token_set"] is False


def test_an_unsaved_token_can_be_tried_against_the_saved_address(connected):
    r = client.post("/api/paperless/test", json={"token": "not-the-right-token-1234"})
    assert r.json()["ok"] is False and r.json()["error_kind"] == "unauthorized"
    assert connected.requests[-1].headers["authorization"] == "Token not-the-right-token-1234"
    # the saved token is untouched
    assert client.post("/api/paperless/test", json={"token": ""}).json()["ok"] is True


def test_a_new_address_needs_its_own_token_and_never_gets_the_saved_one(connected):
    before = len(connected.requests)
    r = client.post("/api/paperless/test", json={"url": OTHER})
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "token"
    assert client.post("/api/paperless/test", json={"url": OTHER, "token": ""}).status_code == 422
    assert len(connected.requests) == before  # nothing was sent anywhere

    r = client.post("/api/paperless/test", json={"url": OTHER, "token": "typed-for-the-other-1234"})
    assert r.status_code == 200
    sent = connected.requests[-1]
    assert sent.url.host == "other.test"
    assert sent.headers["authorization"] == "Token typed-for-the-other-1234"
    assert all(TOKEN not in str(rq.headers) for rq in connected.requests[before:])

    # the saved address (however it is spelled) may use the saved token
    assert client.post("/api/paperless/test", json={"url": BASE + "/"}).json()["ok"] is True


@pytest.mark.parametrize("body", [
    {"url": ""}, {"url": "ftp://x"}, {"url": "http://u:p@host"}, {"url": "http://a b"}, {"url": 5},
    {"token": "bad\ntoken"}, {"token": "x" * 2000}, {"token": 5}, {"other": 1},
])
def test_unusable_input_is_a_422_that_does_not_echo_it(connected, body):
    r = client.post("/api/paperless/test", json={"token": TOKEN, **body} if "token" not in body else body)
    assert r.status_code == 422
    for value in body.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in r.text
    assert TOKEN not in r.text


def test_error_kinds(connected, monkeypatch):
    def check(expected):
        r = client.post("/api/paperless/test")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["version"] is None
        assert body["error_kind"] == expected, body
        assert body["error"]
        return body

    # a wrong token
    save(token="a-different-synthetic-token-42")
    check("unauthorized")
    save(token=TOKEN)
    connected.status_override = 403
    check("unauthorized")
    connected.status_override = 404
    check("not_found")
    connected.status_override = 500
    body = check("other")
    assert "500" in body["error"]
    connected.status_override = 302
    check("other")
    connected.status_override = None

    connected.fail = httpx.ConnectError("All connection attempts failed")
    check("connection")
    connected.fail = httpx.ReadError("reset")
    check("connection")

    tls = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    tls.__cause__ = ssl.SSLCertVerificationError("self-signed certificate")
    connected.fail = tls
    check("tls")
    only_message = httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    connected.fail = only_message
    check("tls")

    for timeout in (httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t")):
        connected.fail = timeout
        check("timeout")
    connected.fail = ValueError("something else entirely")
    check("other")

    connected.fail = None
    connected.body_override = httpx.Response(200, text="<html>hello</html>")  # a web server, not Paperless
    check("other")
    connected.body_override = httpx.Response(200, json={"detail": "hi"})
    check("other")


def test_a_real_refused_connection_is_a_connection_error():
    r = client.post("/api/paperless/test", json={"url": "http://127.0.0.1:9", "token": "typed-token-12345678"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error_kind"] == "connection"
    assert "127.0.0.1" not in r.text and "typed-token" not in r.text  # a fixed message, no address


def test_not_configured_and_no_token_say_so():
    body = client.post("/api/paperless/test").json()
    assert body["ok"] is False and body["error_kind"] == "other" and "URL" in body["error"]
    save(url=BASE)
    body = client.post("/api/paperless/test").json()
    assert body["ok"] is False and body["error_kind"] == "unauthorized" and "token" in body["error"]


def test_errors_carry_neither_the_token_nor_the_exceptions_own_text(connected, caplog):
    connected.fail = httpx.ConnectError(f"boom {TOKEN} http://user:hunter2@internal.test/")
    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/paperless/test")
        r2 = client.post("/api/paperless/test", json={"url": OTHER, "token": "typed-secret-token-777"})
        r3 = client.post("/api/paperless/discover")
        r4 = client.post("/api/paperless/preview-count")
    for resp in (r, r2, r3, r4):
        assert "hunter2" not in resp.text and TOKEN not in resp.text and "boom" not in resp.text
        assert "typed-secret-token-777" not in resp.text
    assert TOKEN not in caplog.text and "typed-secret-token-777" not in caplog.text


# --- discover ---------------------------------------------------------------------------------


def test_discover_lists_types_and_tags_with_counts(connected):
    r = client.post("/api/paperless/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["document_types"] == [{"id": 11, "name": "Invoice", "count": 1}, {"id": 10, "name": "Medical", "count": 2}]
    assert body["tags"] == [
        {"id": 1, "name": "Blood Test", "count": 1},
        {"id": 4, "name": "Imaging", "count": 0},
        {"id": 2, "name": "Medical Report", "count": 1},
        {"id": 3, "name": "Prescription", "count": 0},
        {"id": 5, "name": "Receipt", "count": 1},
    ]
    assert body["total"] == {"document_types": 2, "tags": 5}
    assert body["truncated"] == {"document_types": False, "tags": False}


def test_discover_is_capped_and_searchable_on_a_big_instance(connected):
    for i in range(100, 3100):  # three thousand tags
        connected.tags[i] = f"Label {i}"
    before = len(connected.requests)
    body = client.post("/api/paperless/discover").json()
    assert len(body["tags"]) == 100 and body["total"]["tags"] == 3005 and body["truncated"]["tags"] is True
    assert len(connected.requests) - before == 2  # one page per list, never all of them
    assert all(r.url.params["page_size"] == "100" for r in connected.requests[before:])

    body = client.post("/api/paperless/discover", json={"limit": 200}).json()
    assert len(body["tags"]) == 200

    body = client.post("/api/paperless/discover", json={"q": "  MEDICAL "}).json()
    assert [t["name"] for t in body["tags"]] == ["Medical Report"]
    assert [t["name"] for t in body["document_types"]] == ["Medical"]
    assert body["truncated"] == {"document_types": False, "tags": False}

    body = client.post("/api/paperless/discover", json={"q": "Label 31"}).json()
    assert body["total"]["tags"] == 10 and body["document_types"] == []  # Label 3100 is out of range

    for bad in ({"limit": 201}, {"limit": 0}, {"q": "x" * 101}, {"nope": 1}):
        assert client.post("/api/paperless/discover", json=bad).status_code == 422


def test_discover_needs_a_connection_but_not_the_switch(fake):
    r = client.post("/api/paperless/discover")
    assert r.status_code == 409 and r.json()["detail"]["error_kind"] == "other"
    save(url=BASE, token=TOKEN, enabled=False)
    assert client.post("/api/paperless/discover").status_code == 200


def test_discover_reports_upstream_failures(connected):
    connected.status_override = 401
    r = client.post("/api/paperless/discover")
    assert r.status_code == 502 and r.json()["detail"]["error_kind"] == "unauthorized"
    connected.status_override = None
    connected.fail = httpx.ConnectTimeout("x")
    r = client.post("/api/paperless/discover")
    assert r.status_code == 502 and r.json()["detail"]["error_kind"] == "timeout"


# --- preview-count ----------------------------------------------------------------------------


def test_preview_of_the_default_selection_counts_what_a_sync_finds(connected):
    r = client.post("/api/paperless/preview-count")
    assert r.status_code == 200 and r.json() == {"count": 3, "empty_selection": False}
    assert client.post("/api/paperless/preview-count", json={}).json()["count"] == 3
    # only counts were asked for: page_size=1, and never a page of documents
    for q in connected.doc_queries():
        assert q["page_size"] == "1"
    # ...and the tag ids came from looking the names up, not from listing every tag
    tag_queries = [dict(r.url.params) for r in connected.requests if r.url.path == "/api/tags/"]
    assert tag_queries and all("name__iexact" in q for q in tag_queries)


def test_preview_of_a_candidate_selection(connected):
    def count(**body):
        r = client.post("/api/paperless/preview-count", json=body)
        assert r.status_code == 200, r.text
        return r.json()["count"]

    assert count(match="type_only") == 2
    assert count(match="tags_only") == 2
    assert count(match="type_or_tags") == 3  # 2 + 2, and one document has both
    assert count(match="type_only", document_type="invoice") == 1
    assert count(match="tags_only", tags=["receipt", "Nope"]) == 1
    assert count(match="tags_only", tags=["Nope"]) == 0
    assert count(document_type="", tags=["Receipt"]) == 1
    assert count(document_type="Invoice", tags=["Blood Test"]) == 2
    assert count(tags=["Imaging"]) == 2  # no tag has it; the type still counts: the two Medical documents
    # nothing saved changed
    assert client.get("/api/settings/paperless").json()["values"]["match"] == "type_or_tags"


def test_an_empty_choice_counts_nothing_without_asking_paperless(connected):
    before = len(connected.requests)
    for body in ({"document_type": "", "tags": []}, {"match": "type_only", "document_type": " "},
                 {"match": "tags_only", "tags": []}):
        assert client.post("/api/paperless/preview-count", json=body).json() == {
            "count": 0, "empty_selection": True}
    assert len(connected.requests) == before


def test_preview_input_is_validated(connected):
    for bad in ({"match": "sometimes"}, {"tags": "x"}, {"tags": ["x" * 101]}, {"document_type": "x" * 101},
                {"unknown": 1}):
        assert client.post("/api/paperless/preview-count", json=bad).status_code == 422


def test_preview_needs_a_connection_and_reports_failures(fake, connected):
    connected.fail = httpx.ConnectError("x")
    r = client.post("/api/paperless/preview-count")
    assert r.status_code == 502 and r.json()["detail"]["error_kind"] == "connection"
    connected.fail = None
    save(token="")
    assert client.post("/api/paperless/preview-count").status_code == 409
