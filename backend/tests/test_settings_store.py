"""Settings layers (app > env > default), secrets through the API, startup log."""

import logging

from conftest import PasswordClient
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import reading_settings, settings_store
from app.config import Settings
from app.db import engine
from app.main import app
from app.models import AppSetting

client = PasswordClient(app)
SECRET = "synthetic-token-0123456789abcdef"


def view(section="paperless"):
    r = client.get(f"/api/settings/{section}")
    assert r.status_code == 200
    return r.json()


def stored(key):
    with Session(engine) as s:
        row = s.get(AppSetting, key)
        return row.value if row else None


def test_built_in_defaults_are_generic(monkeypatch):
    for name in ("PAPERLESS_URL", "PAPERLESS_PUBLIC_URL", "OLLAMA_URL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.paperless_url == "" and s.paperless_public_url == ""
    assert s.ollama_url == "http://host.docker.internal:11434"


def test_precedence_app_over_env_over_default(monkeypatch, reload_config):
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    reload_config()
    v = view()
    assert v["values"]["url"] == "" and v["sources"]["url"] == "default"

    monkeypatch.setenv("PAPERLESS_URL", "http://env-paperless:8000")
    reload_config()
    v = view()
    assert v["values"]["url"] == "http://env-paperless:8000" and v["sources"]["url"] == "env"

    r = client.put("/api/settings/paperless", json={"url": "http://app-paperless:8000/"})
    assert r.status_code == 200
    assert r.json()["values"]["url"] == "http://app-paperless:8000"  # normalised
    assert r.json()["sources"]["url"] == "app"

    # clearing the override falls back to the environment, then to the default
    r = client.put("/api/settings/paperless", json={"url": None})
    assert r.json()["values"]["url"] == "http://env-paperless:8000"
    assert r.json()["sources"]["url"] == "env"
    assert stored("paperless.url") is None
    monkeypatch.delenv("PAPERLESS_URL")
    reload_config()
    assert view()["sources"]["url"] == "default"


def test_partial_update_leaves_other_settings_alone():
    client.put("/api/settings/paperless", json={"document_type": "Health"})
    client.put("/api/settings/paperless", json={"tags": ["Lab", " Lab ", "X"]})
    v = view()["values"]
    assert v["document_type"] == "Health" and v["tags"] == ["Lab", "X"]


def test_validation_errors_and_unknown_names():
    r = client.put("/api/settings/paperless", json={"url": "ftp://nope", "bogus": 1})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["detail"]}
    assert fields == {"url", "bogus"}
    assert client.put("/api/settings/general", json={"language": "fr"}).status_code == 422
    assert client.put("/api/settings/uploads", json={"max_file_mb": 0}).status_code == 422
    assert client.put("/api/settings/paperless", json={"url": "http://ok:1"}).status_code == 200
    # all or nothing: a rejected update saves none of its fields
    r = client.put("/api/settings/general", json={"language": "el", "trusted_origins": ["bad"]})
    assert r.status_code == 422
    assert view("general")["values"]["language"] == "en"


def test_internal_and_unknown_sections_are_not_exposed():
    for name in ("auth", "setup", "reading", "nope"):
        assert client.get(f"/api/settings/{name}").status_code == 404
        assert client.put(f"/api/settings/{name}", json={}).status_code == 404
    assert set(client.get("/api/settings").json()) == {"paperless", "general", "uploads"}


def test_secret_is_write_only_and_encrypted_at_rest():
    assert view()["values"]["token_set"] is False
    r = client.put("/api/settings/paperless", json={"token": f"  {SECRET}\n"})
    assert r.status_code == 200
    body = r.json()["values"]
    assert body["token_set"] is True and body["token_last4"] == SECRET[-4:]
    assert body["token_source"] == "app" and "token" not in body
    raw = stored("paperless.token")
    assert raw.startswith("enc:v1:") and SECRET not in raw
    assert settings_store.value("paperless", "token") == SECRET

    # absent = unchanged
    client.put("/api/settings/paperless", json={"document_type": "Health"})
    assert view()["values"]["token_set"] is True
    # "" = clear
    r = client.put("/api/settings/paperless", json={"token": ""})
    assert r.json()["values"]["token_set"] is False
    assert stored("paperless.token") is None


def test_secret_never_appears_in_any_body_or_log(caplog):
    client.put("/api/settings/paperless", json={"token": SECRET})
    with caplog.at_level(logging.DEBUG):
        texts = [
            client.get("/api/settings").text,
            client.get("/api/settings/paperless").text,
            client.get("/api/reading/settings").text,
            client.get("/api/auth/status").text,
            client.get("/api/status").text,
            client.get("/api/health").text,
            # error bodies
            client.put("/api/settings/paperless", json={"token": 12345, "url": SECRET}).text,
            client.put("/api/settings/paperless", json={"token": ["x", SECRET]}).text,
            client.put("/api/settings/paperless", json=[SECRET]).text,
            client.put("/api/settings/paperless", content='{"token": "' + SECRET + '"').text,
            client.put("/api/settings/paperless", json={"token": SECRET, "zzz": SECRET}).text,
            client.put("/api/settings/paperless", json={"tags": SECRET}).text,
            client.get(f"/api/settings/{SECRET}").text,
            client.post("/api/auth/login", json={"password": [SECRET]}).text,
        ]
        logging.getLogger("vibehealth").warning("oops %s", SECRET)
        settings_store.log_sources()
    for text in texts:
        assert SECRET not in text
    assert SECRET not in caplog.text


def test_secret_input_is_validated_without_echo():
    for bad in (123, ["a"], "bad\nvalue-with-newline", "x" * 2000, "   "):
        r = client.put("/api/settings/paperless", json={"token": bad})
        assert r.status_code == 422, bad
        assert str(bad).strip() not in r.text or str(bad).strip() == ""


def test_undecryptable_secret_reads_as_unset_then_env(monkeypatch, reload_config):
    monkeypatch.setenv("SECRET_KEY", "first-synthetic-key")
    reload_config()
    client.put("/api/settings/paperless", json={"token": SECRET})
    assert view()["values"]["token_set"] is True
    monkeypatch.setenv("SECRET_KEY", "second-synthetic-key")
    reload_config()
    client.relogin()  # sessions are signed with a key derived from SECRET_KEY
    v = view()["values"]  # no crash
    assert v["token_set"] is False and v["token_last4"] == "" and v["token_source"] == "default"
    # ...and the environment answers if it has a token
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-synthetic-token-9999")
    reload_config()
    client.relogin()
    v = view()["values"]
    assert v["token_set"] is True and v["token_source"] == "env"
    assert v["token_last4"] == "9999"
    # re-entering works with the new key
    client.put("/api/settings/paperless", json={"token": SECRET})
    assert view()["values"]["token_source"] == "app"


def test_env_secret_is_reported_without_its_value(monkeypatch, reload_config):
    monkeypatch.setenv("PAPERLESS_TOKEN", SECRET)
    reload_config()
    r = client.get("/api/settings")
    assert SECRET not in r.text
    assert r.json()["paperless"]["values"]["token_source"] == "env"


def test_reading_settings_report_sources(monkeypatch, reload_config):
    monkeypatch.setenv("READER_A_MODEL", "env-model:1b")
    reload_config()
    r = client.get("/api/reading/settings").json()
    assert r["settings"]["reader_a_model"] == "env-model:1b"
    assert r["sources"]["reader_a_model"] == "env" and r["sources"]["dpi"] == "default"
    body = dict(r["settings"], dpi=200)
    r = client.put("/api/reading/settings", json=body).json()
    assert r["sources"]["dpi"] == "app" and r["sources"]["reader_a_model"] == "env"
    assert stored("reading.dpi") == "200" and stored("reading.reader_a_model") is None
    # back to the defaults drops the override
    r = client.put("/api/reading/settings", json=r["defaults"]).json()
    assert r["sources"]["dpi"] == "default"
    assert reading_settings.load().dpi == 150


def test_startup_log_lists_names_and_sources_only(monkeypatch, reload_config, caplog):
    monkeypatch.setenv("PAPERLESS_URL", "http://private-host-synthetic:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", SECRET)
    reload_config()
    with caplog.at_level(logging.INFO, logger="vibehealth"):
        settings_store.log_sources()
    assert "paperless: enabled=default url=env public_url=default token=env" in caplog.text
    assert "auth: password_hash=default" in caplog.text
    assert "private-host-synthetic" not in caplog.text and SECRET not in caplog.text


def test_undecryptable_or_invalid_stored_values_are_skipped():
    with Session(engine) as s:
        s.add(AppSetting(key="general.language", value='"fr"'))  # no longer valid
        s.add(AppSetting(key="uploads.max_file_mb", value="not json"))
        s.add(AppSetting(key="paperless.token", value="enc:v1:garbage"))
        s.commit()
    settings_store.clear_cache()
    assert view("general")["sources"]["language"] == "default"
    assert view("uploads")["values"]["max_file_mb"] == 50
    assert view()["values"]["token_set"] is False
