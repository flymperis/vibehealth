"""API docs off, security headers everywhere, cache headers, body size cap and field length limits."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import middleware, security
from app.main import FRONTEND_DIR, app
from app.middleware import CSP
from app.models import Document

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "correct horse battery"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def owner():
    c = TestClient(app)
    r = c.post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": security.current_setup_code()})
    assert r.status_code == 200
    return c


# --- M8: API docs are off ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/docs/", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
                                  "/api/openapi.json", "/api/docs"])
def test_docs_are_not_served(client, path):
    r = client.get(path)
    assert r.status_code == 404, path
    assert "swagger" not in r.text.lower() and "openapi" not in r.text.lower()


def test_dev_docs_can_be_switched_on_by_the_environment():
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/redoc').status_code)\n"
    )
    for value, expected in (("1", "200 200 200"), ("", "404 404 404"), ("0", "404 404 404")):
        env = {**os.environ, "VIBEHEALTH_DEV_DOCS": value}
        out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True,
                             text=True, timeout=120)
        assert out.stdout.split()[-3:] == expected.split(), (value, out.stdout, out.stderr[-500:])


# --- L4: headers on every response --------------------------------------------------------------------------


def assert_headers(r, api=False):
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == CSP
    if api:
        assert "private" in r.headers["cache-control"]


def test_csp_has_exactly_the_wanted_directives():
    directives = dict(d.strip().split(" ", 1) for d in CSP.split(";"))
    assert directives == {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self' 'unsafe-inline'",
        "img-src": "'self' data: blob:",
        "connect-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'none'",
        "frame-ancestors": "'none'",
    }


def test_headers_on_every_kind_of_response(client, owner, tmp_path):
    os.makedirs(os.path.join(FRONTEND_DIR, "assets"), exist_ok=True)
    asset = os.path.join(FRONTEND_DIR, "assets", "app-test.js")
    with open(asset, "w") as f:
        f.write("console.log(1)")
    try:
        responses = {
            "api ok": owner.get("/api/documents"),
            "api 401": client.get("/api/documents"),
            "api 404": owner.get("/api/nope"),
            "api 422": owner.post("/api/auth/login", json={}),
            "health": client.get("/api/health"),
            "spa shell": client.get("/"),
            "spa route": client.get("/documents/3"),
            "asset": client.get("/assets/app-test.js"),
            "missing asset": client.get("/assets/none.js"),
            "docs 404": client.get("/docs"),
            "host refused": client.get("/api/health", headers={"Host": "evil.example"}),
            "origin refused": client.post("/api/auth/logout", headers={"Origin": "http://evil.example"}),
            "too large": client.post("/api/auth/login", content=b"x" * (2 * 1024 * 1024)),
        }
    finally:
        os.remove(asset)
    assert responses["asset"].status_code == 200
    for name, r in responses.items():
        assert_headers(r, api=name.startswith(("api", "health")))
    assert responses["host refused"].status_code == 400
    assert responses["origin refused"].status_code == 403
    assert responses["too large"].status_code == 413


def test_api_responses_are_not_cached(owner):
    for path in ("/api/documents", "/api/auth/status", "/api/health", "/api/settings", "/api/reading/catalog"):
        assert owner.get(path).headers["cache-control"] == "private, no-store", path


def test_the_app_shell_is_revalidated_and_assets_are_not_no_store(client):
    assert client.get("/").headers["cache-control"] == "no-cache, must-revalidate"
    assert "no-store" not in client.get("/assets/none.js").headers.get("cache-control", "")


def test_paperless_thumbnails_and_previews_are_private_no_store(owner, monkeypatch):
    from sqlmodel import Session

    from app import paperless
    from app.db import engine

    with Session(engine) as s:
        doc = Document(paperless_id=99, title="synthetic")
        s.add(doc)
        s.commit()
        doc_id = doc.id

    async def thumbnail(self, pid):
        return b"img", "image/webp"

    async def preview(self, pid):
        return b"%PDF", "application/pdf"

    monkeypatch.setattr(paperless.Paperless, "thumbnail", thumbnail)
    monkeypatch.setattr(paperless.Paperless, "preview", preview)
    t = owner.get(f"/api/documents/{doc_id}/thumbnail")
    p = owner.get(f"/api/documents/{doc_id}/preview")
    assert t.status_code == p.status_code == 200
    assert t.headers["cache-control"] == p.headers["cache-control"] == "private, no-store"
    assert_headers(t) and assert_headers(p)


# --- M9: field lengths --------------------------------------------------------------------------------------------


def test_login_password_length_is_capped(owner):
    long = "x" * (security.MAX_PASSWORD_LENGTH + 1)
    r = TestClient(app).post("/api/auth/login", json={"password": long})
    assert r.status_code == 422 and long not in r.text
    ok = TestClient(app).post("/api/auth/login", json={"password": "x" * security.MAX_PASSWORD_LENGTH})
    assert ok.status_code == 401  # a wrong password of the maximum length is just wrong


@pytest.mark.parametrize("field", ["current", "new", "setup_code"])
def test_change_password_fields_are_capped(owner, field):
    body = {"current": PASSWORD, "new": "n" * 12, "setup_code": ""}
    body[field] = "y" * 257
    r = owner.post("/api/auth/change-password", json=body)
    assert r.status_code == 422 and "y" * 257 not in r.text
    assert owner.get("/api/documents").status_code == 200  # nothing changed, nobody was signed out


def test_a_long_first_password_claim_is_rejected_before_any_work():
    r = TestClient(app).post("/api/auth/change-password", json={"new": "n" * 12, "setup_code": "z" * 300})
    assert r.status_code == 422


# --- M9: body size cap -------------------------------------------------------------------------------------------------


def test_bodies_up_to_the_limit_pass_and_bigger_ones_are_413(client):
    at_limit = json.dumps({"password": "a", "pad": "p" * (middleware.DEFAULT_MAX_BODY - 40)}).encode()
    assert len(at_limit) <= middleware.DEFAULT_MAX_BODY
    r = client.post("/api/auth/login", content=at_limit, headers={"Content-Type": "application/json"})
    assert r.status_code == 200  # open instance: the note about no password
    over = b'{"password": "a", "pad": "' + b"p" * middleware.DEFAULT_MAX_BODY + b'"}'
    r = client.post("/api/auth/login", content=over, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "request body too large"}


@pytest.mark.parametrize("method,path", [("put", "/api/settings/paperless"), ("put", "/api/reading/settings"),
                                         ("post", "/api/paperless/test"), ("patch", "/api/documents/1/ignore"),
                                         ("post", "/api/anything/at/all")])
def test_every_route_has_the_cap(client, method, path):
    r = client.request(method, path, content=b"[" + b"0," * (600 * 1024) + b"0]",
                       headers={"Content-Type": "application/json"})
    assert r.status_code == 413, path


def test_an_invalid_content_length_is_a_400(client):
    r = client.post("/api/auth/login", content=b"{}", headers={"Content-Length": "abc"})
    assert r.status_code in (400, 422)  # h11 may refuse it first; never a 500


def raw_call(path, chunks, headers=()):
    """Drive the ASGI app directly with a chunked body and no Content-Length."""
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"localhost"), (b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"]


def test_a_chunked_body_is_counted_as_it_arrives():
    chunk = b"a" * (256 * 1024)
    assert raw_call("/api/auth/login", [b'{"password": "', chunk, b'"}']) == 422  # ~256 KiB: fine (password too long)
    assert raw_call("/api/auth/login", [b'{"password": "'] + [chunk] * 5 + [b'"}']) == 413


def test_a_lying_content_length_does_not_get_around_the_cap():
    chunk = b"a" * (512 * 1024)
    status = raw_call("/api/auth/login", [b'{"password": "', chunk, chunk, chunk, b'"}'],
                      headers=[(b"content-length", b"20")])
    assert status == 413


def test_per_route_limit_hook(client, monkeypatch):
    """The future upload route can be allowed a larger body without loosening the rest."""
    monkeypatch.setattr(middleware, "_route_limits", {})
    big = b'{"password": "' + b"p" * (3 * 1024 * 1024) + b'"}'
    assert client.post("/api/auth/login", content=big).status_code == 413
    middleware.set_body_limit("/api/auth/login", 4 * 1024 * 1024)
    assert client.post("/api/auth/login", content=big).status_code == 422  # through the cap, then field validation
    assert client.post("/api/auth/logout-all", content=big).status_code == 413  # other routes keep the default
    assert middleware.body_limit_for("/api/auth/login") == 4 * 1024 * 1024
    assert middleware.body_limit_for("/api/health") == middleware.DEFAULT_MAX_BODY
    # the longest prefix wins
    middleware.set_body_limit("/api/auth/login/x", 10)
    assert middleware.body_limit_for("/api/auth/login/x/y") == 10
    assert middleware.body_limit_for("/api/auth/login") == 4 * 1024 * 1024
