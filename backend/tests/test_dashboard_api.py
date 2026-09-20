"""Dashboard summary and examinations endpoints: pure queries, no reading pipeline."""

from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine
from app.main import app
from app.models import Document, DocumentKind, ExtractedValue, ValueStatus

client = TestClient(app)


def make_doc(paperless_id, title, kind=DocumentKind.BLOOD_TEST, doc_date=None, ignored=False):
    with Session(engine) as s:
        doc = Document(paperless_id=paperless_id, title=title, kind=kind, doc_date=doc_date, ignored=ignored)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc.id


def make_value(document_id, test_code, value_text="10", status=ValueStatus.APPROVED, flag="", unit="", ref_range=""):
    with Session(engine) as s:
        v = ExtractedValue(
            document_id=document_id, test_code=test_code, raw_name=test_code or "",
            value_text=value_text, unit=unit, ref_range=ref_range, flag=flag, status=status,
        )
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


# --- empty state ---------------------------------------------------------------


def test_dashboard_summary_empty():
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "recent_documents": [],
        "flagged_values": [],
        "category_counts": [],
        "has_any_approved": False,
    }


def test_examinations_empty_blood_test():
    r = client.get("/api/examinations", params={"kind": "blood_test"})
    assert r.status_code == 200
    assert r.json() == {"kind": "blood_test", "categories": [], "documents": None}


def test_examinations_empty_other_kind():
    r = client.get("/api/examinations", params={"kind": "imaging"})
    assert r.status_code == 200
    assert r.json() == {"kind": "imaging", "categories": None, "documents": []}


# --- dashboard summary -----------------------------------------------------------


def test_dashboard_recent_documents_and_counts():
    make_doc(1, "old blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_doc(2, "new blood test", DocumentKind.BLOOD_TEST, date(2024, 6, 1))
    make_doc(3, "an mri", DocumentKind.IMAGING, date(2024, 3, 1))
    make_doc(4, "hidden", DocumentKind.BLOOD_TEST, date(2024, 12, 1), ignored=True)

    r = client.get("/api/dashboard/summary")
    body = r.json()
    titles = [d["title"] for d in body["recent_documents"]]
    assert titles == ["new blood test", "an mri", "old blood test"]  # newest first, ignored excluded

    counts = {c["kind"]: (c["count"], c["last_date"]) for c in body["category_counts"]}
    assert counts == {
        "blood_test": (2, "2024-06-01"),
        "imaging": (1, "2024-03-01"),
    }
    assert body["has_any_approved"] is False


def test_dashboard_recent_documents_limit():
    for i in range(7):
        make_doc(100 + i, f"doc {i}", DocumentKind.REPORT, date(2024, 1, i + 1))
    r = client.get("/api/dashboard/summary", params={"recent_limit": 3})
    assert len(r.json()["recent_documents"]) == 3


def test_dashboard_flagged_values_only_approved_and_flagged():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "GLU", "130", ValueStatus.APPROVED, flag="H")
    make_value(doc, "UREA", "30", ValueStatus.VERIFIED, flag="H")  # not approved: excluded
    make_value(doc, "CREA", "0.9", ValueStatus.APPROVED, flag="")  # not flagged: excluded

    r = client.get("/api/dashboard/summary")
    body = r.json()
    assert len(body["flagged_values"]) == 1
    flagged = body["flagged_values"][0]
    assert flagged["test_code"] == "GLU"
    assert flagged["name_en"] == "Glucose"
    assert flagged["flag"] == "H"
    assert flagged["document_id"] == doc
    assert body["has_any_approved"] is True


def test_dashboard_flagged_values_limit_and_order():
    doc_old = make_doc(1, "old", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    doc_new = make_doc(2, "new", DocumentKind.BLOOD_TEST, date(2024, 6, 1))
    make_value(doc_old, "GLU", "130", ValueStatus.APPROVED, flag="H")
    make_value(doc_new, "UREA", "5", ValueStatus.APPROVED, flag="L")

    r = client.get("/api/dashboard/summary", params={"flagged_limit": 1})
    flagged = r.json()["flagged_values"]
    assert len(flagged) == 1
    assert flagged[0]["test_code"] == "UREA"  # most recent doc_date first


def test_dashboard_flagged_values_ignored_document_excluded():
    doc = make_doc(1, "hidden", DocumentKind.BLOOD_TEST, date(2024, 1, 1), ignored=True)
    make_value(doc, "GLU", "130", ValueStatus.APPROVED, flag="H")
    r = client.get("/api/dashboard/summary")
    assert r.json()["flagged_values"] == []


# --- examinations: blood tests grouped by category -------------------------------


def test_examinations_blood_test_category_grouping():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "GLU", "100", ValueStatus.APPROVED, unit="mg/dL")  # diabetes
    make_value(doc, "TSH", "2.0", ValueStatus.APPROVED, unit="μIU/mL")  # thyroid
    make_value(doc, "UNKNOWNCODE", "1", ValueStatus.APPROVED)  # not in catalog -> other
    make_value(doc, "CREA", "1.0", ValueStatus.VERIFIED)  # not approved: excluded entirely

    r = client.get("/api/examinations", params={"kind": "blood_test"})
    body = r.json()
    categories = {c["category"]: c["tests"] for c in body["categories"]}
    assert set(categories) == {"diabetes", "thyroid", "other"}
    assert [t["test_code"] for t in categories["diabetes"]] == ["GLU"]
    assert categories["other"][0]["test_code"] == "UNKNOWNCODE"
    all_codes = {t["test_code"] for tests in categories.values() for t in tests}
    assert "CREA" not in all_codes


def test_examinations_blood_test_most_recent_value_per_test():
    doc1 = make_doc(1, "jan", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    doc2 = make_doc(2, "june", DocumentKind.BLOOD_TEST, date(2024, 6, 1))
    doc3 = make_doc(3, "march", DocumentKind.BLOOD_TEST, date(2024, 3, 1))
    make_value(doc1, "GLU", "90", ValueStatus.APPROVED)
    make_value(doc2, "GLU", "110", ValueStatus.APPROVED)
    make_value(doc3, "GLU", "95", ValueStatus.APPROVED)

    r = client.get("/api/examinations", params={"kind": "blood_test"})
    tests = [t for c in r.json()["categories"] for t in c["tests"]]
    [glu] = [t for t in tests if t["test_code"] == "GLU"]
    assert glu["value"] == "110"
    assert glu["document_id"] == doc2
    assert glu["doc_date"] == "2024-06-01"


def test_examinations_blood_test_ignored_document_excluded():
    doc = make_doc(1, "hidden", DocumentKind.BLOOD_TEST, date(2024, 1, 1), ignored=True)
    make_value(doc, "GLU", "100", ValueStatus.APPROVED)
    r = client.get("/api/examinations", params={"kind": "blood_test"})
    assert r.json()["categories"] == []


# --- limit param validation -------------------------------------------------------


def test_flagged_values_rejects_out_of_range_limits():
    for bad_limit in (-1, 0, 99999):
        r = client.get("/api/values/flagged", params={"limit": bad_limit})
        assert r.status_code == 422


def test_dashboard_summary_rejects_out_of_range_recent_limit():
    for bad_limit in (-1, 0, 99999):
        r = client.get("/api/dashboard/summary", params={"recent_limit": bad_limit})
        assert r.status_code == 422


def test_dashboard_summary_rejects_out_of_range_flagged_limit():
    for bad_limit in (-1, 0, 99999):
        r = client.get("/api/dashboard/summary", params={"flagged_limit": bad_limit})
        assert r.status_code == 422


# --- CBC differential pairs: percentage + absolute merged for display -------------


def test_by_test_merges_percentage_and_absolute_pair():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "NEUT", "38.0", ValueStatus.APPROVED, unit="%")
    make_value(doc, "NEUT_ABS", "2.44", ValueStatus.APPROVED, unit="10^3/μL")

    r = client.get("/api/values/by-test")
    assert r.status_code == 200
    codes = [t["test_code"] for t in r.json()]
    assert codes.count("NEUT") == 1
    assert "NEUT_ABS" not in codes  # not double-listed

    [row] = [t for t in r.json() if t["test_code"] == "NEUT"]
    assert row["latest"]["value"] == "38.0"
    assert row["latest"]["unit"] == "%"
    assert row["secondary"]["value"] == "2.44"
    assert row["secondary"]["unit"] == "10^3/μL"
    assert row["secondary_history"][0]["value"] == "2.44"


def test_by_test_percentage_only_no_crash():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "NEUT", "38.0", ValueStatus.APPROVED, unit="%")

    r = client.get("/api/values/by-test")
    assert r.status_code == 200
    [row] = [t for t in r.json() if t["test_code"] == "NEUT"]
    assert row["latest"]["value"] == "38.0"
    assert row["secondary"] is None
    assert row["secondary_history"] is None


def test_by_test_absolute_only_no_crash():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "NEUT_ABS", "2.44", ValueStatus.APPROVED, unit="10^3/μL")

    r = client.get("/api/values/by-test")
    assert r.status_code == 200
    codes = [t["test_code"] for t in r.json()]
    assert codes.count("NEUT_ABS") == 1
    [row] = [t for t in r.json() if t["test_code"] == "NEUT_ABS"]
    assert row["latest"]["value"] == "2.44"
    assert row["secondary"] is None


def test_examinations_merges_percentage_and_absolute_pair():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "NEUT", "38.0", ValueStatus.APPROVED, unit="%")
    make_value(doc, "NEUT_ABS", "2.44", ValueStatus.APPROVED, unit="10^3/μL")

    r = client.get("/api/examinations", params={"kind": "blood_test"})
    tests = [t for c in r.json()["categories"] for t in c["tests"]]
    codes = [t["test_code"] for t in tests]
    assert codes.count("NEUT") == 1
    assert "NEUT_ABS" not in codes

    [row] = [t for t in tests if t["test_code"] == "NEUT"]
    assert row["value"] == "38.0"
    assert row["secondary"]["value"] == "2.44"


def test_examinations_percentage_only_no_crash():
    doc = make_doc(1, "blood test", DocumentKind.BLOOD_TEST, date(2024, 1, 1))
    make_value(doc, "LYMPH", "40.0", ValueStatus.APPROVED, unit="%")

    r = client.get("/api/examinations", params={"kind": "blood_test"})
    tests = [t for c in r.json()["categories"] for t in c["tests"]]
    [row] = [t for t in tests if t["test_code"] == "LYMPH"]
    assert row["secondary"] is None


# --- examinations: other kinds -> plain document list -----------------------------


def test_examinations_other_kind_document_list():
    make_doc(1, "old report", DocumentKind.REPORT, date(2024, 1, 1))
    make_doc(2, "new report", DocumentKind.REPORT, date(2024, 6, 1))
    make_doc(3, "hidden report", DocumentKind.REPORT, date(2024, 12, 1), ignored=True)
    make_doc(4, "an mri", DocumentKind.IMAGING, date(2024, 3, 1))

    r = client.get("/api/examinations", params={"kind": "report"})
    body = r.json()
    assert body["categories"] is None
    titles = [d["title"] for d in body["documents"]]
    assert titles == ["new report", "old report"]
