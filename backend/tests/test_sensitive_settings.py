"""Settings that steer where the server connects or whom it trusts: refused while the app is
open, and confirmed with the current password once there is one. The helper endpoints may only
use the saved configuration while the app is open."""

import httpx
import pytest
from fake_paperless import BASE, TOKEN, FakePaperless
from fastapi.testclient import TestClient

from app import reading_settings, security, settings_store
from app.main import app
from app.ollama import Ollama, OllamaError
from app.paperless import Paperless

PASSWORD = "correct horse battery"
OTHER_URL = "http://elsewhere.test:9000"


def new_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def open_client():
    return new_client()


@pytest.fixture
def owner():
    """A password is set and this client is signed in."""
    c = new_client()
    r = c.post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": security.current_setup_code()})
    assert r.status_code == 200
    return c


def put(c, section, **body):
    return c.put(f"/api/settings/{section}", json=body)


def effective(section, name):
    return settings_store.value(section, name)


# --- open mode: refused -----------------------------------------------------------------------


@pytest.mark.parametrize("section,body", [
    ("paperless", {"url": OTHER_URL}),
    ("paperless", {"public_url": OTHER_URL}),
    ("general", {"trusted_origins": ["https://evil.example"]}),
    ("general", {"allowed_hosts": ["evil.example"]}),
])
def test_sensitive_changes_are_refused_while_open(open_client, section, body):
    before = settings_store.resolve(section, use_cache=False).values
    r = put(open_client, section, **body)
    assert r.status_code == 403
    assert "password" in r.json()["detail"] and OTHER_URL not in r.text and "evil" not in r.text
    assert settings_store.resolve(section, use_cache=False).values == before  # nothing was saved


def test_a_token_sent_with_a_new_address_is_refused_too(open_client):
    r = put(open_client, "paperless", url=OTHER_URL, token="a-synthetic-token-1234567890")
    assert r.status_code == 403
    assert not settings_store.value("paperless", "token")


def test_a_saved_token_does_not_turn_the_refusal_into_a_token_question(open_client):
    """With a token in place, a new address would normally ask for the token again (422). While
    open the answer is the refusal (403), so nobody is sent on a round trip that cannot succeed."""
    settings_store.update("paperless", {"url": "http://saved.test:8000", "token": "a-synthetic-token-1234567890"})
    r = put(open_client, "paperless", url=OTHER_URL)
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert effective("paperless", "url") == "http://saved.test:8000"


def test_with_a_password_the_token_rule_still_applies_after_confirmation(owner):
    settings_store.update("paperless", {"url": "http://saved.test:8000", "token": "a-synthetic-token-1234567890"})
    r = put(owner, "paperless", url=OTHER_URL, current_password=PASSWORD)
    assert r.status_code == 422 and r.json()["detail"][0]["field"] == "token"
    r = put(owner, "paperless", url=OTHER_URL, token="another-synthetic-token-12345", current_password=PASSWORD)
    assert r.status_code == 200 and effective("paperless", "url") == OTHER_URL


def test_reading_address_is_refused_while_open(open_client):
    s = open_client.get("/api/reading/settings").json()["settings"]
    r = open_client.put("/api/reading/settings", json=dict(s, ollama_url="http://evil.example:11434"))
    assert r.status_code == 403
    assert reading_settings.load().ollama_url == s["ollama_url"]


def test_other_fields_and_unchanged_values_still_save_while_open(open_client):
    assert put(open_client, "paperless", document_type="Health", token="a-synthetic-token-1234567890").status_code == 200
    assert put(open_client, "general", language="el").status_code == 200
    # a whole form sent back with the sensitive fields as they already are: no change
    current = open_client.get("/api/settings/general").json()["values"]
    assert put(open_client, "general", **current).status_code == 200
    p = open_client.get("/api/settings/paperless").json()["values"]
    p = {k: v for k, v in p.items() if not k.startswith("token_")}
    assert put(open_client, "paperless", **p).status_code == 200
    s = open_client.get("/api/reading/settings").json()["settings"]
    assert open_client.put("/api/reading/settings", json=dict(s, dpi=200)).status_code == 200
    assert reading_settings.load().dpi == 200


def test_unusable_input_is_a_422_not_a_403(open_client):
    assert put(open_client, "paperless", url="ftp://nope").status_code == 422
    assert put(open_client, "general", trusted_origins=["bad"]).status_code == 422
    assert put(open_client, "general", allowed_hosts=["bad host"]).status_code == 422


def test_clearing_a_saved_address_is_a_change_too(open_client):
    settings_store.update("paperless", {"url": "http://saved.test:8000"})  # as if saved earlier
    assert put(open_client, "paperless", url=None).status_code == 403
    assert effective("paperless", "url") == "http://saved.test:8000"


def test_the_environment_still_sets_them_without_a_password(monkeypatch, reload_config):
    monkeypatch.setenv("PAPERLESS_URL", "http://env-paperless:8000")
    monkeypatch.setenv("PAPERLESS_PUBLIC_URL", "https://docs.example.com")
    monkeypatch.setenv("OLLAMA_URL", "http://env-ollama:11434")
    monkeypatch.setenv("TRUSTED_ORIGINS", '["https://health.example.com"]')
    reload_config()
    assert Paperless().url == "http://env-paperless:8000"
    assert Paperless.public_link(7).startswith("https://docs.example.com/")
    assert reading_settings.load().ollama_url == "http://env-ollama:11434"
    assert effective("general", "trusted_origins") == ["https://health.example.com"]


# --- with a password: confirmed by the current password -----------------------------------------


def test_a_password_holder_must_confirm_with_the_current_password(owner):
    r = put(owner, "paperless", url=OTHER_URL)
    assert r.status_code == 403 and r.json()["detail"][0]["field"] == "current_password"
    r = put(owner, "paperless", url=OTHER_URL, current_password="not the password")
    assert r.status_code == 403 and r.json()["detail"][0]["field"] == "current_password"
    assert effective("paperless", "url") == ""
    r = put(owner, "paperless", url=OTHER_URL, current_password=PASSWORD)
    assert r.status_code == 200 and effective("paperless", "url") == OTHER_URL
    # the confirmation is not a setting
    assert "current_password" not in r.text and "current_password" not in owner.get("/api/settings/paperless").text


@pytest.mark.parametrize("section,field,value", [
    ("paperless", "public_url", OTHER_URL),
    ("general", "trusted_origins", ["https://health.example.com"]),
    ("general", "allowed_hosts", ["health.example.com"]),
])
def test_every_sensitive_field_needs_the_password(owner, section, field, value):
    assert put(owner, section, **{field: value}).status_code == 403
    assert put(owner, section, **{field: value, "current_password": PASSWORD}).status_code == 200
    assert effective(section, field) in (value, OTHER_URL)


def test_reading_address_needs_the_password(owner):
    s = owner.get("/api/reading/settings").json()["settings"]
    changed = dict(s, ollama_url="http://gpu-box:11434")
    assert owner.put("/api/reading/settings", json=changed).status_code == 403
    assert owner.put("/api/reading/settings", json=dict(changed, current_password="nope nope nope")).status_code == 403
    assert reading_settings.load().ollama_url == s["ollama_url"]
    r = owner.put("/api/reading/settings", json=dict(changed, current_password=PASSWORD))
    assert r.status_code == 200 and reading_settings.load().ollama_url == "http://gpu-box:11434"
    # other reading settings need nothing
    assert owner.put("/api/reading/settings", json=dict(changed, dpi=210)).status_code == 200


def test_partial_updates_of_other_fields_need_no_password(owner):
    assert put(owner, "paperless", document_type="Health").status_code == 200
    assert put(owner, "general", language="el").status_code == 200
    assert put(owner, "uploads", max_file_mb=20).status_code == 200


def test_guessing_the_password_through_settings_is_throttled(owner):
    for _ in range(5):
        assert put(owner, "paperless", url=OTHER_URL, current_password="wrong guess").status_code == 403
    r = put(owner, "paperless", url=OTHER_URL, current_password=PASSWORD)
    assert r.status_code == 429 and "retry-after" in r.headers
    assert effective("paperless", "url") == ""


def test_a_request_without_a_session_gets_nothing(owner):
    r = put(new_client(), "paperless", url=OTHER_URL, current_password=PASSWORD)
    assert r.status_code == 401


def test_a_confirmation_of_the_wrong_type_or_size_is_just_wrong(owner):
    assert put(owner, "paperless", url=OTHER_URL, current_password=12345).status_code == 403
    assert put(owner, "paperless", url=OTHER_URL, current_password="x" * 5000).status_code == 403
    assert put(owner, "paperless", url=OTHER_URL, current_password=None).status_code == 403


# --- the Paperless helpers in open mode --------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    return FakePaperless().standard().install(monkeypatch)


def test_open_mode_tests_only_the_saved_configuration(fake, open_client):
    settings_store.update("paperless", {"url": BASE, "token": TOKEN})
    for body in ({"url": OTHER_URL, "token": "typed-token-0123456789"}, {"url": OTHER_URL},
                 {"token": "typed-token-0123456789"}):
        r = open_client.post("/api/paperless/test", json=body)
        assert r.status_code == 403, body
        assert "typed-token" not in r.text and "elsewhere" not in r.text
    assert fake.requests == []  # nothing left the server
    # without a body, or with the saved address, it works
    assert open_client.post("/api/paperless/test").json()["ok"] is True
    assert open_client.post("/api/paperless/test", json={"url": BASE + "/"}).json()["ok"] is True
    assert open_client.post("/api/paperless/test", json={"token": ""}).json()["ok"] is True
    assert open_client.post("/api/paperless/discover").status_code == 200
    assert open_client.post("/api/paperless/preview-count", json={}).status_code == 200


def test_with_a_password_the_helpers_may_try_other_values(fake, owner):
    settings_store.update("paperless", {"url": BASE, "token": TOKEN})
    r = owner.post("/api/paperless/test",
                   json={"url": OTHER_URL, "token": "typed-token-0123456789", "current_password": PASSWORD})
    assert r.status_code == 200
    assert fake.requests and fake.requests[-1].url.host == "elsewhere.test"


def test_another_paperless_address_needs_the_current_password_even_with_a_password(fake, owner):
    settings_store.update("paperless", {"url": BASE, "token": TOKEN})
    body = {"url": OTHER_URL, "token": "typed-token-0123456789"}
    for extra in ({}, {"current_password": ""}, {"current_password": "wrong password here"}):
        r = owner.post("/api/paperless/test", json={**body, **extra})
        assert r.status_code == 403, extra
        assert "typed-token" not in r.text
    assert fake.requests == []  # nothing left the server
    # the saved address needs no confirmation
    assert owner.post("/api/paperless/test").json()["ok"] is True
    assert owner.post("/api/paperless/test", json={"url": BASE + "/", "token": TOKEN}).json()["ok"] is True


def test_helper_errors_are_fixed_strings(fake, open_client):
    settings_store.update("paperless", {"url": BASE, "token": TOKEN})
    fake.fail = httpx.ConnectError("boom at http://192.0.2.7:1234/secret-path")
    for path in ("test", "discover", "preview-count"):
        r = open_client.post(f"/api/paperless/{path}", json={})
        assert "192.0.2.7" not in r.text and "secret-path" not in r.text and "paperless.test" not in r.text, path


# --- Ollama test-connection ---------------------------------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    calls = []

    async def models(self):
        calls.append(self.url)
        return ["a:1", "b:2"]

    monkeypatch.setattr(Ollama, "models", models)
    return calls


def test_reading_test_connection_open_mode_uses_only_the_saved_address(ollama, open_client):
    saved = reading_settings.load().ollama_url
    r = open_client.post("/api/reading/test-connection", json={"ollama_url": "http://evil.example:11434"})
    assert r.status_code == 403 and ollama == []
    assert open_client.post("/api/reading/test-connection").json()["ok"] is True
    assert open_client.post("/api/reading/test-connection", json={"ollama_url": saved + "/"}).json()["ok"] is True
    assert ollama == [saved, saved]


def test_reading_test_connection_with_a_password_may_try_another_address(ollama, owner):
    r = owner.post("/api/reading/test-connection",
                   json={"ollama_url": "http://gpu-box:11434", "current_password": PASSWORD})
    assert r.status_code == 200 and r.json()["models"] == ["a:1", "b:2"]
    assert ollama == ["http://gpu-box:11434"]
    bad = owner.post("/api/reading/test-connection", json={"ollama_url": "not a url"}).json()
    assert bad["ok"] is False and "not a url" not in str(bad)


def test_another_ollama_address_needs_the_current_password(ollama, owner):
    for extra in ({}, {"current_password": ""}, {"current_password": "wrong password here"}):
        r = owner.post("/api/reading/test-connection", json={"ollama_url": "http://gpu-box:11434", **extra})
        assert r.status_code == 403, extra
    assert ollama == []  # never connected
    saved = reading_settings.load().ollama_url
    assert owner.post("/api/reading/test-connection", json={"ollama_url": saved}).json()["ok"] is True
    assert owner.post("/api/reading/test-connection").json()["ok"] is True


def test_ollama_test_connection_is_rate_limited(ollama, owner):
    codes = [owner.post("/api/reading/test-connection").status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_reading_test_connection_errors_are_generic(open_client, monkeypatch):
    real = httpx.AsyncClient

    def boom(request):
        raise httpx.ConnectError("refused by http://198.51.100.3:11434/api/tags")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(boom), **kw))
    body = open_client.post("/api/reading/test-connection").json()
    assert body["ok"] is False and body["error"] == "Ollama is not reachable at the configured address."
    assert "198.51.100.3" not in body["error"]


def test_reading_test_connection_survives_an_odd_reply(open_client, monkeypatch):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"models": [{"name": "ok:1"}, {"x": 1}, None]}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    r = open_client.post("/api/reading/test-connection")
    assert r.status_code == 200 and r.json()["models"] == ["ok:1"]
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    r = open_client.post("/api/reading/test-connection")
    assert r.status_code == 200 and r.json()["ok"] is False


def test_ollama_error_type_is_what_the_route_reports(open_client, monkeypatch):
    async def models(self):
        raise OllamaError("Ollama did not answer in time.")

    monkeypatch.setattr(Ollama, "models", models)
    assert open_client.post("/api/reading/test-connection").json()["error"] == "Ollama did not answer in time."
