"""The Paperless connection as settings: validation, token safety, match modes, kind map, links."""

import asyncio
import logging

import pytest
from fake_paperless import BASE, TOKEN, FakePaperless
from conftest import PasswordClient
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Document, DocumentKind
from app.paperless import Paperless
from app.services import sync_documents

client = PasswordClient(app)

# The mapping that used to be hard-coded in services.py.
OLD_TAG_TO_KIND = {
    "blood test": DocumentKind.BLOOD_TEST,
    "medical report": DocumentKind.REPORT,
    "prescription": DocumentKind.PRESCRIPTION,
    "imaging": DocumentKind.IMAGING,
}


def put(**body):
    return client.put("/api/settings/paperless", json=body)


def view():
    return client.get("/api/settings/paperless").json()


def errors(r):
    return {e["field"]: e["message"] for e in r.json()["detail"]}


def connect(monkeypatch, fake=None, **extra):
    """A saved connection to the fake Paperless (url + token saved in the app)."""
    fake = (fake or FakePaperless().standard()).install(monkeypatch)
    assert put(url=BASE, token=TOKEN, **extra).status_code == 200
    return fake


def synced():
    with Session(engine) as s:
        return {d.paperless_id: d for d in s.exec(select(Document))}


def run_sync():
    return asyncio.run(sync_documents())


# --- defaults ---------------------------------------------------------------------


def test_new_fields_and_their_defaults():
    v = view()
    assert v["values"]["enabled"] is True and v["sources"]["enabled"] == "default"
    assert v["values"]["match"] == "type_or_tags" and v["sources"]["match"] == "default"
    assert v["values"]["sync_interval_minutes"] == 240
    assert v["values"]["document_type"] == "Medical"
    assert v["values"]["tags"] == ["Blood Test", "Medical Report", "Prescription", "Imaging"]
    assert {k.lower(): DocumentKind(x) for k, x in v["values"]["kind_map"].items()} == OLD_TAG_TO_KIND
    assert v["sources"]["kind_map"] == "default"


def test_interval_comes_from_the_environment_until_saved(monkeypatch, reload_config):
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "60")
    reload_config()
    v = view()
    assert v["values"]["sync_interval_minutes"] == 60 and v["sources"]["sync_interval_minutes"] == "env"
    assert put(sync_interval_minutes=0).json()["sources"]["sync_interval_minutes"] == "app"
    assert view()["values"]["sync_interval_minutes"] == 0


# --- validation -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "ftp://host", "host:8000", "http://", "http:///path", "javascript:alert(1)", "//host",
    "http://user:pw@host:8000", "http://user@host", "http://ho st:8000", "http://host\n:8000",
    "http://host:8000/a b", "http://host:8000/\x00", "http://host:99999", "http://host:0",
    "http://host:8000?x=1", "http://host:8000/#frag", "http://ho$t", "http://hôst", "http://host\\evil",
    "http://" + "a" * 2100, "http://[::1", 5,
])
def test_url_and_public_url_are_validated(bad):
    for name in ("url", "public_url"):
        r = put(**{name: bad})
        assert r.status_code == 422, (name, bad)
        assert name in errors(r)
    assert view()["values"]["url"] == ""


@pytest.mark.parametrize("good,saved", [
    ("http://localhost:8000", "http://localhost:8000"),
    ("  https://paperless.example.com/  ", "https://paperless.example.com"),
    ("http://192.0.2.5:8000/paperless/", "http://192.0.2.5:8000/paperless"),
    ("http://[::1]:8000", "http://[::1]:8000"),
    ("http://paperless_web:8000", "http://paperless_web:8000"),
    ("", ""),
])
def test_good_urls_are_normalised(good, saved):
    assert put(url=good, public_url=good).json()["values"]["url"] == saved


def test_empty_selection_is_rejected_only_while_enabled():
    r = put(document_type="", tags=[])
    assert r.status_code == 422 and "selection" in errors(r)
    assert "type or at least one tag" in errors(r)["selection"]
    assert view()["values"]["document_type"] == "Medical"  # nothing was saved

    assert put(document_type="", tags=[], enabled=False).status_code == 200
    assert put(enabled=True).status_code == 422  # turning it back on with nothing chosen
    assert put(enabled=True, tags=["Lab"]).status_code == 200

    # per mode
    assert put(match="type_only").status_code == 422  # no type left to match on
    assert put(match="type_only", document_type="Medical").status_code == 200
    assert put(match="tags_only", tags=[]).status_code == 422
    assert put(match="tags_only", tags=["Lab"]).status_code == 200
    assert put(match="type_or_tags", tags=[], document_type=" ").status_code == 422
    assert put(match="sometimes").status_code == 422


def test_kind_map_must_use_valid_kinds():
    r = put(kind_map={"Lab": "not_a_kind"})
    assert r.status_code == 422 and any(k.startswith("kind_map") for k in errors(r))
    assert put(kind_map={"Lab": "blood_test", "lab": "report"}).status_code == 422  # same tag twice
    assert put(kind_map={"": "report"}).status_code == 422
    assert put(kind_map=["x"]).status_code == 422
    r = put(kind_map={" Lab ": "blood_test", "Scan": "imaging", "Misc": "other"})
    assert r.status_code == 200
    assert view()["values"]["kind_map"] == {"Lab": "blood_test", "Scan": "imaging", "Misc": "other"}
    assert put(kind_map=None).json()["sources"]["kind_map"] == "default"


@pytest.mark.parametrize("minutes,ok", [(0, True), (5, True), (240, True), (10080, True),
                                        (1, False), (4, False), (-1, False), (10081, False), ("x", False)])
def test_sync_interval_bounds(minutes, ok):
    assert (put(sync_interval_minutes=minutes).status_code == 200) is ok


# --- the token goes only where it was saved ----------------------------------------------


def test_changing_the_url_requires_the_token_again():
    assert put(url="http://a.test:8000", token=TOKEN).status_code == 200
    r = put(url="http://b.test:8000")
    assert r.status_code == 422 and "token" in errors(r) and "URL changed" in errors(r)["token"]
    assert view()["values"]["url"] == "http://a.test:8000"  # unchanged

    assert put(url="http://a.test:8000/").status_code == 200  # same address: fine
    assert put(url="http://a.test:8000", document_type="Health").status_code == 200

    r = put(url="http://b.test:8000", token="another-synthetic-token-9999")
    assert r.status_code == 200 and r.json()["values"]["token_last4"] == "9999"

    # clearing the token together with the URL is fine: nothing is left to send
    r = put(url="http://c.test:8000", token="")
    assert r.status_code == 200 and r.json()["values"]["token_set"] is False


def test_removing_the_saved_url_falls_back_to_env_and_asks_for_the_token(monkeypatch, reload_config):
    monkeypatch.setenv("PAPERLESS_URL", "http://env.test:8000")
    reload_config()
    assert put(url="http://a.test:8000", token=TOKEN).status_code == 200
    assert put(url=None).status_code == 422  # env address differs from the saved one
    r = put(url=None, token="token-for-the-env-address-1234")
    assert r.status_code == 200 and r.json()["values"]["url"] == "http://env.test:8000"


def test_an_environment_token_is_not_sent_to_a_new_address_either(monkeypatch, reload_config):
    monkeypatch.setenv("PAPERLESS_URL", "http://env.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-synthetic-token-7777")
    reload_config()
    assert put(url="http://elsewhere.test:8000").status_code == 422
    assert put(url="http://env.test:8000/").status_code == 200  # the same address
    assert put(url="http://elsewhere.test:8000", token="typed-synthetic-token-5555").status_code == 200


def test_the_client_never_sends_the_saved_token_to_another_address(monkeypatch):
    fake = connect(monkeypatch)
    p = Paperless(url="http://other.test:9000")
    assert p.url == "http://other.test:9000" and p.token == ""
    assert Paperless(url="http://other.test:9000", token="typed-1234567890").token == "typed-1234567890"
    assert Paperless(url=BASE + "/").token == TOKEN and Paperless().token == TOKEN
    assert fake.requests == []


def test_pagination_ignores_next_links_pointing_elsewhere(monkeypatch):
    fake = connect(monkeypatch)
    fake.next_host = "http://evil.test"
    for i in range(6, 236):  # 230 more documents of the Medical type: three pages of 100
        fake.doc(i, f"Doc {i}", 10)
    rows = asyncio.run(Paperless().medical_documents())
    assert len(rows) == 233
    assert {r.url.host for r in fake.requests} == {"paperless.test"}
    assert all(r.headers["authorization"] == f"Token {TOKEN}" for r in fake.requests)
    pages = [r.url.params.get("page") for r in fake.requests if r.url.path == "/api/documents/"]
    assert pages[:3] == [None, "2", "3"]  # the first request is the same as it always was


def test_the_token_is_not_in_bodies_or_logs(monkeypatch, caplog):
    connect(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        texts = [client.get("/api/settings/paperless").text, client.get("/api/status").text,
                 put(url="http://z.test:1").text, put(url="http://z.test:1", token=["x", TOKEN]).text,
                 put(match=TOKEN).text, put(kind_map={TOKEN: "nope"}).text]
        run_sync()
    for text in texts:
        assert TOKEN not in text
    assert TOKEN not in caplog.text


# --- which documents sync -----------------------------------------------------------------


def test_default_equals_the_old_behaviour_query_for_query(monkeypatch):
    fake = connect(monkeypatch)
    result = run_sync()
    assert (result["seen"], result["created"], result["updated"]) == (3, 3, 0)
    assert set(synced()) == {1, 2, 3}
    # the same two document queries as before, and nothing else about the documents
    assert fake.doc_queries() == [
        {"document_type__name__iexact": "Medical", "ordering": "-created", "page_size": "100"},
        {"tags__id__in": "1,4,2,3", "ordering": "-created", "page_size": "100"},
    ]
    assert run_sync()["created"] == 0  # and a second run finds the same three


def test_env_only_configuration_syncs_the_same_three(monkeypatch, reload_config):
    fake = FakePaperless().standard().install(monkeypatch)
    monkeypatch.setenv("PAPERLESS_URL", BASE)
    monkeypatch.setenv("PAPERLESS_TOKEN", TOKEN)
    reload_config()
    assert view()["sources"]["url"] == "env" and view()["values"]["token_source"] == "env"
    assert run_sync()["seen"] == 3 and set(synced()) == {1, 2, 3}
    assert len(fake.doc_queries()) == 2


def test_match_type_only(monkeypatch):
    fake = connect(monkeypatch, match="type_only")
    assert run_sync()["seen"] == 2 and set(synced()) == {1, 3}
    assert fake.doc_queries() == [
        {"document_type__name__iexact": "Medical", "ordering": "-created", "page_size": "100"}
    ]


def test_match_tags_only(monkeypatch):
    fake = connect(monkeypatch, match="tags_only")
    assert run_sync()["seen"] == 2 and set(synced()) == {1, 2}
    assert fake.doc_queries() == [{"tags__id__in": "1,4,2,3", "ordering": "-created", "page_size": "100"}]


def test_match_uses_the_chosen_type_and_tags_and_ignores_unknown_tags(monkeypatch):
    connect(monkeypatch, match="tags_only", tags=["receipt", "No Such Tag"])
    assert run_sync()["seen"] == 1 and set(synced()) == {4}  # case ignored
    put(match="type_or_tags", document_type="Invoice", tags=["Imaging"])
    assert run_sync()["seen"] == 1  # the invoice; nobody has the Imaging tag


def test_the_selection_is_read_at_call_time(monkeypatch):
    connect(monkeypatch)
    assert run_sync()["seen"] == 3
    put(match="type_only")
    assert run_sync()["seen"] == 2


def test_kind_map_decides_the_kind(monkeypatch):
    connect(monkeypatch)
    run_sync()
    assert synced()[1].kind == DocumentKind.BLOOD_TEST and synced()[2].kind == DocumentKind.REPORT
    assert synced()[3].kind == DocumentKind.OTHER  # a type and no tag
    put(kind_map={"medical report": "imaging", "Blood Test": "other"})
    assert run_sync()["updated"] == 2
    assert synced()[1].kind == DocumentKind.OTHER and synced()[2].kind == DocumentKind.IMAGING


def test_documents_out_of_the_selection_stay(monkeypatch):
    connect(monkeypatch)
    run_sync()
    put(match="type_only")
    run_sync()
    assert set(synced()) == {1, 2, 3}


# --- links ----------------------------------------------------------------------------------


def test_links_use_the_public_url_and_fall_back_to_the_url():
    put(url="http://internal.test:8000", token=TOKEN)
    assert Paperless.public_link(7) == "http://internal.test:8000/documents/7/details"
    put(public_url="https://paperless.example.com/")
    assert Paperless.public_link(7) == "https://paperless.example.com/documents/7/details"
    put(public_url="")
    assert Paperless.public_link(7) == "http://internal.test:8000/documents/7/details"


def test_document_links_in_the_api_follow_the_setting(monkeypatch):
    connect(monkeypatch, public_url="https://p.example.com")
    with Session(engine) as s:
        s.add(Document(paperless_id=42, title="synthetic"))
        s.commit()
    doc = client.get("/api/documents").json()[0]
    assert doc["paperless_link"] == "https://p.example.com/documents/42/details"
    put(public_url="")
    assert client.get("/api/documents").json()[0]["paperless_link"] == f"{BASE}/documents/42/details"
