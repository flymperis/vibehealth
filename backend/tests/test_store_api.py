"""Saving values, re-reading, review actions and settings, through the API."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Document, ExtractedValue, ExtractionRun, RunStatus, ValueStatus
from app.reading import save_candidates
from app.verify import NEEDS_REVIEW, VERIFIED, Candidate

client = TestClient(app)


def cand(code, value, status=VERIFIED):
    return Candidate(code, code or "?", value, "mg/dl", "10 - 50", "", status, "test", 1, value, value)


@pytest.fixture
def doc_id():
    with Session(engine) as s:
        doc = Document(paperless_id=999, title="synthetic")
        s.add(doc)
        s.commit()
        return doc.id


def values(doc_id):
    with Session(engine) as s:
        return s.exec(select(ExtractedValue).where(ExtractedValue.document_id == doc_id)).all()


def test_rerun_keeps_approved_values(doc_id):
    save_candidates(doc_id, None, [cand("UREA", "30"), cand("CREA", "0.9", NEEDS_REVIEW)])
    urea = next(v for v in values(doc_id) if v.test_code == "UREA")
    assert client.post(f"/api/values/{urea.id}/approve").status_code == 200

    counts = save_candidates(doc_id, None, [cand("UREA", "31"), cand("CREA", "1.0"), cand(None, "7", NEEDS_REVIEW)])
    assert counts == {"verified": 1, "needs_review": 1, "kept_approved": 1}
    rows = {(v.test_code, v.status): v.value_text for v in values(doc_id)}
    assert rows == {
        ("UREA", ValueStatus.APPROVED): "30",
        ("CREA", ValueStatus.VERIFIED): "1.0",
        (None, ValueStatus.NEEDS_REVIEW): "7",
    }


def test_one_approved_value_per_test(doc_id):
    save_candidates(doc_id, None, [cand("UREA", "30")])
    with Session(engine) as s:
        s.add(ExtractedValue(document_id=doc_id, test_code="UREA", value_text="31", status=ValueStatus.VERIFIED))
        s.commit()
    first, second = sorted(values(doc_id), key=lambda v: v.id)
    assert client.post(f"/api/values/{first.id}/approve").status_code == 200
    r = client.post(f"/api/values/{second.id}/approve")
    assert r.status_code == 409
    # the database refuses it too
    with Session(engine) as s, pytest.raises(IntegrityError):
        row = s.get(ExtractedValue, second.id)
        row.status = ValueStatus.APPROVED
        s.add(row)
        s.commit()
    # after rejecting the first, the second can be approved
    assert client.post(f"/api/values/{first.id}/reject").json()["status"] == "rejected"
    assert client.post(f"/api/values/{second.id}/approve").status_code == 200


def test_edit_then_approve(doc_id):
    save_candidates(doc_id, None, [cand(None, "5", NEEDS_REVIEW)])
    [row] = values(doc_id)
    assert client.post(f"/api/values/{row.id}/approve").status_code == 422  # no test chosen
    assert client.post(f"/api/values/{row.id}/approve", json={"test_code": "NOPE"}).status_code == 422
    r = client.post(f"/api/values/{row.id}/approve", json={"test_code": "GLU", "value": "130,5", "ref_range": "70 - 110"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "approved" and body["value_num"] == 130.5 and body["flag"] == "H"
    assert body["name_en"] == "Glucose"


def test_approve_all_verified_and_clear(doc_id):
    save_candidates(doc_id, None, [cand("UREA", "30"), cand("CREA", "0.9"), cand("K", "9", NEEDS_REVIEW)])
    r = client.post(f"/api/documents/{doc_id}/values/approve-verified")
    assert r.json() == {"approved": 2, "skipped": 0}
    r = client.delete(f"/api/documents/{doc_id}/values")
    assert r.json() == {"deleted": 1}
    data = client.get(f"/api/documents/{doc_id}/values").json()
    assert {v["test_code"] for v in data["values"]} == {"UREA", "CREA"}
    assert data["document"]["reading"]["approved"] == 2
    listed = client.get("/api/documents").json()
    assert listed[0]["reading"]["state"] == "done"


def test_document_reading_state_not_read(doc_id):
    listed = client.get("/api/documents").json()
    assert listed[0]["reading"]["state"] == "not_read"


def test_clear_everything_shows_not_read(doc_id):
    with Session(engine) as s:
        s.add(ExtractionRun(document_id=doc_id, status=RunStatus.DONE, verified=1))
        s.commit()
    save_candidates(doc_id, None, [cand("UREA", "30")])
    assert client.get(f"/api/documents/{doc_id}").json()["reading"]["state"] == "done"
    client.delete(f"/api/documents/{doc_id}/values")
    assert client.get(f"/api/documents/{doc_id}").json()["reading"]["state"] == "not_read"
    assert client.get(f"/api/documents/{doc_id}/values").json()["last_run"]["status"] == "cleared"


def test_settings_roundtrip_and_validation():
    data = client.get("/api/reading/settings").json()
    s = data["settings"]
    assert s["reader_a_model"] == "qwen3.5:4b" and s["dpi"] == 150 and s["fallback_dpis"] == [100, 200]
    assert s["auto_read_after_sync"] is False and s["use_paperless_text"] is True

    bad = dict(s, dpi=1000, keep_alive="soon", ollama_url="ftp://x", reader_b_model="bad model!")
    r = client.put("/api/reading/settings", json=bad)
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["detail"]}
    assert fields == {"dpi", "keep_alive", "ollama_url", "reader_b_model"}
    assert client.put("/api/reading/settings", json=dict(s, unknown=1)).status_code == 422

    r = client.put("/api/reading/settings", json=dict(s, dpi=200, fallback_dpis=[150, 150], keep_alive="0"))
    assert r.status_code == 200
    assert r.json()["settings"]["fallback_dpis"] == [150]
    assert client.get("/api/reading/settings").json()["settings"]["dpi"] == 200
    # back to the defaults: nothing stays stored
    client.put("/api/reading/settings", json=data["defaults"])
    assert client.get("/api/reading/settings").json()["settings"] == data["defaults"]


def test_read_refused_when_disabled(doc_id):
    s = client.get("/api/reading/settings").json()["settings"]
    client.put("/api/reading/settings", json=dict(s, enabled=False))
    r = client.post(f"/api/documents/{doc_id}/read")
    assert r.status_code == 409
    client.put("/api/reading/settings", json=s)


def test_connection_failure_has_a_message():
    r = client.post("/api/reading/test-connection", json={"ollama_url": "http://127.0.0.1:9"})
    body = r.json()
    assert body["ok"] is False and body["error"]
    assert client.post("/api/reading/test-connection", json={"ollama_url": "nope"}).json()["ok"] is False


def test_flagged_values_only_approved_and_out_of_range(doc_id):
    save_candidates(doc_id, None, [cand("GLU", "130"), cand("UREA", "30")])
    glu, urea = values(doc_id)
    # give them ref ranges + flags as approve() would compute from an edit
    client.post(f"/api/values/{glu.id}/approve", json={"ref_range": "70 - 110"})
    client.post(f"/api/values/{urea.id}/approve", json={"ref_range": "10 - 50"})
    r = client.get("/api/values/flagged")
    assert r.status_code == 200
    codes = {v["test_code"]: v["flag"] for v in r.json()}
    assert codes == {"GLU": "H"}  # urea (30) is within 10-50, so not flagged


def test_flagged_values_excludes_ignored_documents(doc_id):
    save_candidates(doc_id, None, [cand("GLU", "999")])
    [glu] = values(doc_id)
    client.post(f"/api/values/{glu.id}/approve", json={"ref_range": "70 - 110"})
    assert len(client.get("/api/values/flagged").json()) == 1
    client.post(f"/api/documents/{doc_id}/ignore")
    assert client.get("/api/values/flagged").json() == []


def test_values_by_test_groups_and_sorts_history(doc_id):
    save_candidates(doc_id, None, [cand("UREA", "30")])
    [urea] = values(doc_id)
    client.post(f"/api/values/{urea.id}/approve")

    with Session(engine) as s:
        doc2 = Document(paperless_id=1000, title="second")
        s.add(doc2)
        s.commit()
        doc2_id = doc2.id
    save_candidates(doc2_id, None, [cand("UREA", "35")])
    [urea2] = values(doc2_id)
    client.post(f"/api/values/{urea2.id}/approve")

    r = client.get("/api/values/by-test")
    assert r.status_code == 200
    [test] = [t for t in r.json() if t["test_code"] == "UREA"]
    assert test["name_en"] == "Urea"
    assert len(test["history"]) == 2
    assert {p["document_id"] for p in test["history"]} == {doc_id, doc2_id}
    assert test["latest"]["document_id"] in {doc_id, doc2_id}


def test_unknown_api_path_is_404():
    assert client.get("/api/nothing-here").status_code == 404
    assert client.post("/api/reading/nope").status_code == 404
    assert client.delete("/api/nothing/here").status_code == 404
    assert client.get("/documents/1").status_code == 200  # SPA route
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/reading/catalog").status_code == 200
