"""Text reports (imaging, medical opinions, prescriptions): migration 3, reading by kind, the summary, the
lab-page heuristic and the API. Ollama is replaced by a fake; the text is invented."""

import asyncio
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from app import migrations, reading, worker
from app.db import engine
from app.main import app
from app.migrations import MIGRATIONS
from app.models import (
    LAB_KINDS,
    Document,
    DocumentKind,
    DocumentReport,
    DocumentText,
    ExtractedValue,
    ExtractionRun,
    ValueStatus,
    reads_lab_values,
)
from app.ollama import OllamaError, PageError
from app.report import clean_summary, looks_like_lab_page, summary_input
from tests.test_migration_uploads import dump, fresh_shape, shape
from tests.test_migrations import legacy_db, make_engine, version

client = TestClient(app)

IMAGING_PAGE = """Ultrasound of the upper abdomen (invented sample)
Liver: normal size and echo pattern, no focal lesion.
Gallbladder: a small polyp on the wall, maximum diameter 3.5 mm.
Right carotid: intima-media thickness 0.9 mm, no significant stenosis 10-20%.
Conclusion: small gallbladder wall polyp, follow-up in 6 months."""

LAB_PAGE = """Hemoglobin (Αιμοσφαιρίνη) 14.8 g/dL 13.0 - 17.0
Hematocrit (Αιματοκρίτης) 44.1 % 40.0 - 52.0
White cells (Λευκά) 6.2 10^3/μL 4.0 - 10.0
Platelets (Αιμοπετάλια) 250 10^3/μL 150 - 400
MCV 88 fL 80 - 100"""


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (595, 842), "white").save(buf, "PNG")
    return buf.getvalue()


def pdf(pages=2) -> bytes:
    images = [Image.new("RGB", (595, 842), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return buf.getvalue()


class FakePaperless:
    async def download(self, paperless_id, original=True):
        return pdf(2), "application/pdf"

    async def document(self, paperless_id):
        return {"content": ""}


class FakeOllama:
    """Records which reader was used; page 2 of the document is a lab page."""

    calls: list = []
    installed = ["qwen3.5:4b"]  # no glm-ocr: a text report must not need reader B
    summary: object = {"conclusion": "Small gallbladder wall polyp.", "key_findings": ["Gallbladder polyp, max 3.5 mm"]}

    def __init__(self, *args):
        pass

    async def models(self):
        return self.installed

    async def read_page_text(self, model, image):
        FakeOllama.calls.append("text")
        return (IMAGING_PAGE if len([c for c in FakeOllama.calls if c == "text"]) == 1 else LAB_PAGE), False

    async def summarize(self, model, text, kind):
        FakeOllama.calls.append(("summary", kind, text))
        if isinstance(FakeOllama.summary, Exception):
            raise FakeOllama.summary
        return FakeOllama.summary

    async def read_rows(self, model, image):
        FakeOllama.calls.append("rows")
        return []

    async def read_text(self, model, image):
        FakeOllama.calls.append("glm")
        return ""


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(reading, "Paperless", FakePaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    FakeOllama.calls = []
    FakeOllama.installed = ["qwen3.5:4b"]
    FakeOllama.summary = {"conclusion": "Small gallbladder wall polyp.", "key_findings": ["Gallbladder polyp, max 3.5 mm"]}


def make_doc(kind, paperless_id=7):
    with Session(engine) as s:
        doc = Document(paperless_id=paperless_id, title="synthetic report", kind=kind)
        s.add(doc)
        s.commit()
        return doc.id


def read(doc_id, force_lab=False):
    return asyncio.run(reading.read_document(doc_id, {}, force_lab))


# --- the kind mapping ---------------------------------------------------------------


def test_the_kind_mapping():
    assert LAB_KINDS == {DocumentKind.BLOOD_TEST, DocumentKind.OTHER}
    assert reads_lab_values(DocumentKind.BLOOD_TEST) and reads_lab_values(DocumentKind.OTHER)
    for kind in (DocumentKind.IMAGING, DocumentKind.REPORT, DocumentKind.PRESCRIPTION):
        assert not reads_lab_values(kind)


# --- migration 3 ----------------------------------------------------------------------


def test_migration_3_adds_two_tables_and_touches_nothing_else(tmp_path):
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path), MIGRATIONS[:2])  # a database as release 2 left it
    assert version(path) == 2
    with sqlite3.connect(path) as c:
        c.execute("DROP TABLE IF EXISTS document_texts")
        c.execute("DROP TABLE IF EXISTS document_reports")
    before = dump(path)

    result = migrations.run(make_engine(path))
    assert result["applied"] == [3] and version(path) == 3
    assert result["backup"] and "pre-v3-" in result["backup"]  # a database with data is copied first
    assert dump(path) == before  # no existing table changed
    assert shape(path) == fresh_shape(tmp_path)  # and the result is what a fresh install has
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM document_texts").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_3_is_harmless_where_the_tables_exist(tmp_path):
    path = str(tmp_path / "fresh.db")
    migrations.run(make_engine(path))
    before = shape(path)
    with sqlite3.connect(path) as c:
        migrations._text_reports(c)
        migrations._text_reports(c)
    assert shape(path) == before


# --- reading by kind ------------------------------------------------------------------


def test_an_imaging_report_is_transcribed_and_summarised_not_read_for_lab_values():
    doc_id = make_doc(DocumentKind.IMAGING)
    summary = read(doc_id)
    assert summary["status"] == "done" and summary["verified"] == 0 and summary["needs_review"] == 0
    kinds = [c if isinstance(c, str) else c[0] for c in FakeOllama.calls]
    assert kinds == ["text", "text", "summary"]  # no lab rows, no reader B
    assert FakeOllama.calls[-1][1] == "imaging report"
    assert "[page 1]" in FakeOllama.calls[-1][2] and "polyp" in FakeOllama.calls[-1][2]

    with Session(engine) as s:
        pages = s.exec(select(DocumentText).order_by(DocumentText.page)).all()
        report = s.get(DocumentReport, doc_id)
        assert s.exec(select(ExtractedValue)).all() == []
    assert [p.page for p in pages] == [1, 2] and "polyp" in pages[0].text
    assert report.summary_status == "ok" and report.auto_generated is True
    assert report.conclusion == "Small gallbladder wall polyp."
    assert json.loads(report.key_findings) == ["Gallbladder polyp, max 3.5 mm"]
    assert json.loads(report.lab_pages) == [2]  # the second page of the sample is a lab table


@pytest.mark.parametrize("kind", [DocumentKind.REPORT, DocumentKind.PRESCRIPTION])
def test_the_other_report_kinds_are_read_as_text(kind):
    read(make_doc(kind))
    assert "rows" not in FakeOllama.calls and "glm" not in FakeOllama.calls and "text" in FakeOllama.calls


@pytest.mark.parametrize("kind", [DocumentKind.BLOOD_TEST, DocumentKind.OTHER])
def test_lab_kinds_still_use_the_lab_readers(kind):
    FakeOllama.installed = ["qwen3.5:4b", "glm-ocr:latest"]
    read(make_doc(kind))
    assert "rows" in FakeOllama.calls and "glm" in FakeOllama.calls and "text" not in FakeOllama.calls
    with Session(engine) as s:
        assert s.exec(select(DocumentReport)).all() == []


def test_a_person_can_ask_for_the_lab_reading_of_a_report():
    FakeOllama.installed = ["qwen3.5:4b", "glm-ocr:latest"]
    read(make_doc(DocumentKind.IMAGING), force_lab=True)
    assert "rows" in FakeOllama.calls and "text" not in FakeOllama.calls


def test_reading_again_removes_the_junk_a_lab_reading_left_but_keeps_approved_values():
    doc_id = make_doc(DocumentKind.IMAGING)
    with Session(engine) as s:
        s.add(ExtractedValue(document_id=doc_id, raw_name="Unknown", value_text="0,050 cm", status=ValueStatus.NEEDS_REVIEW))
        s.add(ExtractedValue(document_id=doc_id, raw_name="Unknown 2", value_text="x", status=ValueStatus.REJECTED))
        s.add(ExtractedValue(document_id=doc_id, test_code="HGB", raw_name="Hemoglobin", value_text="14.8",
                             status=ValueStatus.APPROVED))
        s.commit()
    read(doc_id)
    with Session(engine) as s:
        left = s.exec(select(ExtractedValue)).all()
    assert [(v.test_code, v.status) for v in left] == [("HGB", "approved")]


def test_reading_again_replaces_the_text_and_the_summary():
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)
    FakeOllama.calls = []
    FakeOllama.summary = {"conclusion": "Second.", "key_findings": []}
    read(doc_id)
    with Session(engine) as s:
        assert len(s.exec(select(DocumentText)).all()) == 2
        assert s.exec(select(DocumentReport)).one().conclusion == "Second."


# --- the summary fails: the text stays ------------------------------------------------


@pytest.mark.parametrize("failure", [OllamaError("Ollama did not answer in time."), PageError("empty answer"),
                                     ValueError("boom at http://198.51.100.5:11434/api/chat")])
def test_a_failed_summary_keeps_the_text_and_says_so(failure):
    FakeOllama.summary = failure
    doc_id = make_doc(DocumentKind.IMAGING)
    summary = read(doc_id)  # does not raise
    assert summary["status"] == "done" and summary["error"] == ""
    with Session(engine) as s:
        report = s.get(DocumentReport, doc_id)
        assert len(s.exec(select(DocumentText)).all()) == 2
        assert s.exec(select(ExtractionRun)).one().status == "done"
    assert report.summary_status == "failed" and report.conclusion == "" and json.loads(report.key_findings) == []
    assert report.summary_error  # visible in the app ...
    assert "198.51.100.5" not in report.summary_error  # ... and never the other end's text or an address

    # reading again (the "read again" button) retries the summary
    FakeOllama.summary = {"conclusion": "Fine now.", "key_findings": ["a"]}
    read(doc_id)
    with Session(engine) as s:
        assert s.get(DocumentReport, doc_id).summary_status == "ok"


def test_an_empty_answer_is_marked_empty():
    FakeOllama.summary = {"conclusion": "  ", "key_findings": ["", "  "]}
    doc_id = make_doc(DocumentKind.REPORT)
    read(doc_id)
    with Session(engine) as s:
        assert s.get(DocumentReport, doc_id).summary_status == "empty"


def test_a_report_where_no_page_could_be_read_is_an_error_and_keeps_the_earlier_text(monkeypatch):
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)

    class Blind(FakeOllama):
        async def read_page_text(self, model, image):
            raise PageError("empty answer")

    monkeypatch.setattr(reading, "Ollama", Blind)
    with pytest.raises(reading.ReadingError, match="no page could be read"):
        read(doc_id)
    with Session(engine) as s:
        assert len(s.exec(select(DocumentText)).all()) == 2  # the earlier reading is still there


def test_clean_summary_and_the_input_are_bounded():
    conclusion, findings = clean_summary({"conclusion": "  a \n b ", "key_findings": ["x", "x", "", *[f"f{i}" for i in range(20)]]})
    assert conclusion == "a b" and findings[:2] == ["x", "f0"] and len(findings) == 8
    text = summary_input({1: "A" * 5000, 2: "Z" * 5000}, 3000)
    assert len(text) < 3100 and text.startswith("[page 1]") and text.endswith("Z" * 100) and "[...]" in text


# --- the lab-page heuristic ---------------------------------------------------------------


def test_a_lab_table_is_recognised():
    assert looks_like_lab_page(LAB_PAGE)
    assert looks_like_lab_page("\n".join(["Ουρία 30 mg/dl 10 - 50"] * 4))


def test_narrative_text_is_not_a_lab_page():
    assert not looks_like_lab_page(IMAGING_PAGE)
    assert not looks_like_lab_page("")
    assert not looks_like_lab_page("Hemoglobin 14.8 g/dL 13.0 - 17.0\nHematocrit 44 % 40 - 52")  # two lines: a mention
    assert not looks_like_lab_page("\n".join(["Stenosis 50-70% of the lumen"] * 6))  # a range, but no result besides it


# --- the API --------------------------------------------------------------------------------


def test_the_api_shows_the_report_and_the_read_mode():
    doc_id = make_doc(DocumentKind.IMAGING)
    other = make_doc(DocumentKind.BLOOD_TEST, paperless_id=8)
    read(doc_id)

    listed = {d["id"]: d for d in client.get("/api/documents").json()}
    assert listed[doc_id]["read_mode"] == "text" and listed[other]["read_mode"] == "lab"
    assert listed[doc_id]["report"] == {
        "status": "ok", "findings": 1, "conclusion": "Small gallbladder wall polyp."}
    assert listed[other]["report"] is None
    exams = client.get("/api/examinations", params={"kind": "imaging"}).json()
    assert exams["documents"][0]["report"]["findings"] == 1

    body = client.get(f"/api/documents/{doc_id}/report").json()
    assert body["document"]["read_mode"] == "text"
    r = body["report"]
    assert r["auto"] is True and r["summary_status"] == "ok" and r["lab_pages"] == [2]
    assert r["conclusion"] == "Small gallbladder wall polyp." and r["key_findings"] == ["Gallbladder polyp, max 3.5 mm"]
    assert [p["page"] for p in r["pages"]] == [1, 2] and "polyp" in r["pages"][0]["text"]
    assert client.get(f"/api/documents/{other}/report").json()["report"] is None
    assert client.get("/api/documents/999/report").status_code == 404


def test_read_as_lab_queues_a_lab_reading():
    doc_id = make_doc(DocumentKind.IMAGING)
    assert client.post(f"/api/documents/{doc_id}/read", params={"as_lab": "true"}).json() == {
        "queued": True, "already": False}
    assert doc_id in worker._force_lab
    worker._force_lab.clear()
    worker.state["reading"]["queue"].clear()
