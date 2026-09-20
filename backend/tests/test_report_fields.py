"""Kind-specific fields of text reports, the checks on them, `other` routing, search and migration 4.
Ollama is replaced by a fake; every text and name here is invented."""

import asyncio
import io
import json
import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from app import migrations, ollama, reading, report, search, security, settings_store, worker
from app.db import engine
from app.main import app
from app.migrations import MIGRATIONS
from app.models import (
    Document,
    DocumentKind,
    DocumentReport,
    DocumentSearch,
    DocumentText,
    ExtractedValue,
    ExtractionRun,
    ValueStatus,
)
from app.report_specs import GENERIC, IMAGING, REPORT, spec_for
from app.report_specs import PRESCRIPTION as PRESCRIPTION_SPEC
from app.textfold import fold, fold_words, nfc
from tests.test_migration_uploads import dump, fresh_shape, shape
from tests.test_migrations import legacy_db, make_engine, version

client = TestClient(app)

NARRATIVE = """Ultrasound of the thyroid (invented sample)
Right lobe 40 x 15 x 14 mm, left lobe 38 x 14 x 13 mm. A nodule in the left lobe, maximum diameter 3,5 mm.
Conclusion: small left thyroid nodule, follow-up in 12 months."""

LAB_PAGE = """Hemoglobin (Αιμοσφαιρίνη) 14.8 g/dL 13.0 - 17.0
Hematocrit (Αιματοκρίτης) 44.1 % 40.0 - 52.0
White cells (Λευκά) 6.2 10^3/μL 4.0 - 10.0
Platelets (Αιμοπετάλια) 250 10^3/μL 150 - 400
MCV 88 fL 80 - 100"""

PRESCRIPTION = """ΣΥΝΤΑΓΗ (δείγμα)
Ιατρός: Δρ. Νίκος Παπαδόπουλος
Ημερομηνία: 12/03/2025
Φάρμακο: ΠΑΡΑΔΕΙΓΜΑ 500 mg επικαλυμμένα δισκία
Δραστική ουσία: Παρακεταμόλη
Δοσολογία: 1 δισκίο 3 φορές την ημέρα
Ποσότητα: 1 κουτί
Φάρμακο: ΔΟΚΙΜΙΟΝ 20 mg καψάκια
Δοσολογία: 1 καψάκιο το πρωί"""


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (595, 842), "white").save(buf, "PNG")
    return buf.getvalue()


def pdf(pages) -> bytes:
    images = [Image.new("RGB", (595, 842), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return buf.getvalue()


class FakePaperless:
    async def download(self, paperless_id, original=True):
        return pdf(len(FakeOllama.page_texts)), "application/pdf"

    async def document(self, paperless_id):
        return {"content": ""}


class FakeOllama:
    page_texts: list = [NARRATIVE]
    answer: object = {}
    calls: list = []
    installed = ["qwen3.5:4b", "glm-ocr:latest"]

    def __init__(self, *args):
        pass

    async def models(self):
        return self.installed

    async def read_page_text(self, model, image):
        n = sum(1 for c in FakeOllama.calls if c == "text")
        FakeOllama.calls.append("text")
        return FakeOllama.page_texts[n % len(FakeOllama.page_texts)], False

    async def summarize(self, model, text, spec):
        FakeOllama.calls.append(("summary", spec))
        if isinstance(FakeOllama.answer, Exception):
            raise FakeOllama.answer
        return FakeOllama.answer

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
    FakeOllama.page_texts = [NARRATIVE]
    FakeOllama.answer = {}
    yield
    worker._force.clear()
    worker.state["reading"]["queue"].clear()


_next_id = iter(range(100, 10_000))


def make_doc(kind, title="synthetic document", doc_date=None, ignored=False):
    with Session(engine) as s:
        doc = Document(paperless_id=next(_next_id), title=title, kind=kind, doc_date=doc_date, ignored=ignored)
        s.add(doc)
        s.commit()
        return doc.id


def read(doc_id, force=None):
    return asyncio.run(reading.read_document(doc_id, {}, force))


def stored(doc_id) -> DocumentReport:
    with Session(engine) as s:
        return s.get(DocumentReport, doc_id)


def details(doc_id) -> dict:
    return json.loads(stored(doc_id).details)


def run_routes(doc_id) -> list[str]:
    with Session(engine) as s:
        return [r.route for r in s.exec(select(ExtractionRun).where(ExtractionRun.document_id == doc_id).order_by(ExtractionRun.id))]


# --- the specs -------------------------------------------------------------------------------------


def test_each_kind_has_its_spec_and_the_others_use_the_generic_one():
    assert spec_for(DocumentKind.IMAGING) is IMAGING and spec_for("imaging") is IMAGING
    assert spec_for(DocumentKind.REPORT) is REPORT and spec_for(DocumentKind.PRESCRIPTION) is PRESCRIPTION_SPEC
    assert spec_for(DocumentKind.OTHER) is GENERIC and spec_for("blood_test") is GENERIC
    for spec in (IMAGING, REPORT, PRESCRIPTION_SPEC, GENERIC):
        assert spec.schema["type"] == "object" and set(spec.schema["required"]) == set(spec.schema["properties"])
        assert spec.name in spec.prompt and spec.num_predict >= 1024
    assert {"modality", "regions", "measurements", "conclusion", "key_findings"} == set(IMAGING.schema["properties"])
    assert {"doctor", "specialty", "diagnoses", "recommendations", "follow_up", "conclusion"} <= set(REPORT.schema["properties"])
    assert {"prescriber", "date", "medications"} == set(PRESCRIPTION_SPEC.schema["properties"])


# --- a tolerant reading of the answer -----------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], "text", 7, {}, {"conclusion": None, "key_findings": "one", "modality": 3},
                                 {"measurements": "x", "regions": {"a": 1}, "diagnoses": 5, "follow_up": [1],
                                  "medications": {"name": "x"}, "prescriber": [], "date": {}}])
@pytest.mark.parametrize("kind", [DocumentKind.IMAGING, DocumentKind.REPORT, DocumentKind.PRESCRIPTION, DocumentKind.OTHER])
def test_a_strange_answer_never_raises_and_leaves_out_what_it_cannot_use(kind, raw):
    out = report.clean(kind, raw, {1: NARRATIVE})
    assert out["error"] == "" and out["status"] in ("ok", "empty")
    assert isinstance(out["conclusion"], str) and isinstance(out["key_findings"], list) and isinstance(out["details"], dict)
    json.dumps(out)  # storable


def test_one_finding_as_a_plain_string_counts_as_a_list_of_one():
    assert report.clean(DocumentKind.OTHER, {"conclusion": "c", "key_findings": "one finding"}, {})["key_findings"] == ["one finding"]


def test_the_json_object_is_found_in_a_wrapped_answer_and_a_non_object_is_refused():
    assert ollama._json_object('{"a": 1}') == {"a": 1}
    assert ollama._json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert ollama._json_object('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    for bad in ("", "no json here", "[1, 2]", '{"a": ', "42"):
        with pytest.raises(ollama.StructuredOutputError):
            ollama._json_object(bad)


def test_a_bad_json_answer_is_a_failed_summary_and_keeps_the_text():
    FakeOllama.answer = ollama.StructuredOutputError(ollama.BAD_JSON)
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)
    assert stored(doc_id).summary_status == "failed" and details(doc_id) == {}
    with Session(engine) as s:
        assert len(s.exec(select(DocumentText)).all()) == 1


# --- imaging -----------------------------------------------------------------------------------------


IMAGING_ANSWER = {
    "modality": "Ultrasound", "regions": ["thyroid", "thyroid", ""],
    "measurements": [
        {"label": "left lobe nodule, maximum diameter", "value": "3.5", "unit": "mm"},  # 3,5 in the text
        {"label": "right lobe length", "value": "40", "unit": "mm"},
        {"label": "invented", "value": "7.7", "unit": "mm"},  # not in the text: dropped
        {"label": "no number", "value": "small", "unit": ""},
        {"label": "", "value": "15", "unit": "mm"},
    ],
    "conclusion": "Small left thyroid nodule.", "key_findings": ["Nodule, left lobe, 3.5 mm"],
}


def test_imaging_fields_are_stored_and_a_measurement_needs_its_number_in_the_text():
    FakeOllama.answer = IMAGING_ANSWER
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)
    d = details(doc_id)
    assert d["modality"] == "ultrasound" and d["regions"] == ["thyroid"]
    assert d["measurements"] == [
        {"label": "left lobe nodule, maximum diameter", "value": "3.5", "unit": "mm"},
        {"label": "right lobe length", "value": "40", "unit": "mm"},
    ]
    assert FakeOllama.calls[-1][1] is IMAGING  # the imaging prompt and schema were used


@pytest.mark.parametrize("said, wanted", [("Ultrasound", "ultrasound"), ("υπερηχογράφημα", "ultrasound"), ("US", "ultrasound"),
                                          ("MRI", "mri"), ("Μαγνητική τομογραφία", "mri"), ("CT scan", "ct"),
                                          ("Αξονική", "ct"), ("X-ray", "xray"), ("Ακτινογραφία θώρακος", "xray"),
                                          ("DEXA", "other"), ("", ""), (None, "")])
def test_modality_words_become_one_of_five_values(said, wanted):
    assert report.clean_modality(said) == wanted


# --- medical opinion ---------------------------------------------------------------------------------------


def test_report_fields_and_a_follow_up_date_must_be_printed_in_the_text():
    FakeOllama.page_texts = ["Γνωμάτευση (δείγμα). Επανεξέταση στις 3 Οκτωβρίου 2025. Ιατρός: Μαρία Δοκιμαστική."]
    FakeOllama.answer = {
        "doctor": "Μαρία Δοκιμαστική", "specialty": "Ενδοκρινολόγος", "diagnoses": ["Οζώδης βρογχοκήλη", "Οζώδης βρογχοκήλη"],
        "recommendations": ["Παρακολούθηση"], "follow_up": {"text": "Επανεξέταση", "date": "2025-10-03"},
        "conclusion": "Καλοήθης εικόνα.", "key_findings": [],
    }
    doc_id = make_doc(DocumentKind.REPORT)
    read(doc_id)
    d = details(doc_id)
    assert d["doctor"] == "Μαρία Δοκιμαστική" and d["specialty"] == "Ενδοκρινολόγος"
    assert d["diagnoses"] == ["Οζώδης βρογχοκήλη"] and d["recommendations"] == ["Παρακολούθηση"]
    assert d["follow_up"] == {"text": "Επανεξέταση", "date": "2025-10-03"}  # "3 Οκτωβρίου 2025" is printed

    FakeOllama.answer = {**FakeOllama.answer, "follow_up": {"text": "Επανεξέταση", "date": "2025-11-03"}}
    read(doc_id)
    assert details(doc_id)["follow_up"] == {"text": "Επανεξέταση", "date": ""}  # a date nobody printed is dropped


@pytest.mark.parametrize("text, iso", [("Date 2025-03-12", "2025-03-12"), ("Ημ. 12/03/2025", "2025-03-12"),
                                       ("12.3.25", "2025-03-12"), ("12 Μαρτίου 2025", "2025-03-12"),
                                       ("12 March 2025", "2025-03-12")])
def test_dates_are_found_in_the_usual_greek_and_english_forms(text, iso):
    assert date.fromisoformat(iso) in report.dates_in_text(fold(nfc(text)))


# --- prescriptions: nothing that is not in the text ---------------------------------------------------------


PRESCRIPTION_ANSWER = {
    "prescriber": "Δρ. Νίκος Παπαδόπουλος", "date": "2025-03-12",
    "medications": [
        {"name": "ΠΑΡΑΔΕΙΓΜΑ", "active_substance": "παρακεταμολη", "strength": "500 mg",  # no accent, other case: found
         "dose_instruction": "1 δισκίο 3 φορές την ημέρα", "duration_or_quantity": "1 κουτί"},
        {"name": "ΔΟΚΙΜΙΟΝ", "active_substance": "Ομεπραζόλη",  # invented
         "strength": "20 mg", "dose_instruction": "1 καψάκιο το πρωί", "duration_or_quantity": "30 καψάκια"},  # last: invented
        {"name": "ΦΑΝΤΑΣΜΑ", "active_substance": "", "strength": "", "dose_instruction": "2 φορές", "duration_or_quantity": ""},
        {"name": "", "active_substance": "", "strength": "", "dose_instruction": "", "duration_or_quantity": ""},
    ],
}


def test_a_medication_field_that_is_not_in_the_text_is_dropped_and_flagged():
    FakeOllama.page_texts = [PRESCRIPTION]
    FakeOllama.answer = PRESCRIPTION_ANSWER
    doc_id = make_doc(DocumentKind.PRESCRIPTION)
    read(doc_id)
    d = details(doc_id)
    first, second = d["medications"]
    assert first == {"name": "ΠΑΡΑΔΕΙΓΜΑ", "active_substance": "παρακεταμολη", "strength": "500 mg",
                     "dose_instruction": "1 δισκίο 3 φορές την ημέρα", "duration_or_quantity": "1 κουτί", "unverified": []}
    assert second["name"] == "ΔΟΚΙΜΙΟΝ" and second["strength"] == "20 mg" and second["dose_instruction"] == "1 καψάκιο το πρωί"
    assert second["active_substance"] == "" and second["duration_or_quantity"] == ""  # dropped, never shown
    assert second["unverified"] == ["active_substance", "duration_or_quantity"]
    assert len(d["medications"]) == 2 and d["dropped_medications"] == 1  # "ΦΑΝΤΑΣΜΑ" is not in the text at all
    assert d["prescriber"] == "Δρ. Νίκος Παπαδόπουλος" and d["date"] == "2025-03-12" and d["unverified"] == []
    assert stored(doc_id).summary_status == "ok" and FakeOllama.calls[-1][1] is PRESCRIPTION_SPEC


def test_a_prescription_where_nothing_could_be_confirmed_is_empty_but_says_what_was_left_out():
    FakeOllama.page_texts = [PRESCRIPTION]
    FakeOllama.answer = {"prescriber": "Δρ. Άγνωστος", "date": "2024-01-01", "medications": [
        {"name": "ΑΛΛΟ", "active_substance": "", "strength": "10 mg", "dose_instruction": "", "duration_or_quantity": ""}]}
    doc_id = make_doc(DocumentKind.PRESCRIPTION)
    read(doc_id)
    d = details(doc_id)
    assert d["medications"] == [] and d["dropped_medications"] == 1 and d["prescriber"] == "" and d["date"] == ""
    assert d["unverified"] == ["prescriber", "date"] and stored(doc_id).summary_status == "empty"


def test_whole_words_only_so_a_dose_is_not_found_inside_another_number():
    hay = fold_words("Φάρμακο: Χ 25 mg δισκία")
    assert report._found("25 mg", hay) and not report._found("5 mg", hay) and not report._found("δισκ", hay)
    assert report._found("Χ", hay) and not report._found("", hay)


# --- the medication list --------------------------------------------------------------------------------------


def prescription(doc_date, meds, title="rx", ignored=False, when=""):
    doc_id = make_doc(DocumentKind.PRESCRIPTION, title=title, doc_date=doc_date, ignored=ignored)
    full = [{"name": "", "active_substance": "", "strength": "", "dose_instruction": "", "duration_or_quantity": "",
             "unverified": [], **m} for m in meds]
    with Session(engine) as s:
        s.add(DocumentReport(document_id=doc_id, summary_status="ok",
                             details=json.dumps({"medications": full, "date": when}, ensure_ascii=False)))
        s.commit()
    return doc_id


def test_the_medication_list_groups_by_name_and_takes_the_newest_prescription():
    today = date(2025, 6, 1)
    old = prescription(date(2025, 4, 10), [{"name": "Παράδειγμα", "strength": "250 mg"}])
    new = prescription(date(2025, 5, 20), [{"name": "ΠΑΡΑΔΕΙΓΜΑ", "strength": "500 mg", "unverified": ["dose_instruction"]},
                                            {"active_substance": "Δοκιμιόνη", "dose_instruction": "1 το πρωί"}])
    prescription(date(2024, 1, 1), [{"name": "Παλιό"}])  # too old
    prescription(None, [{"name": "Χωρίς ημερομηνία"}])  # no date
    prescription(date(2025, 5, 25), [{"name": "Κρυμμένο"}], ignored=True)
    prescription(None, [{"name": "Από κείμενο"}], when="2025-05-30")  # the date printed on it counts
    with Session(engine) as s:
        result = report.current_medications(s, today)
    names = [(m["name"] or m["active_substance"], m["date"]) for m in result["medications"]]
    assert names == [("Από κείμενο", "2025-05-30"), ("Δοκιμιόνη", "2025-05-20"), ("ΠΑΡΑΔΕΙΓΜΑ", "2025-05-20")]
    first = result["medications"][2]
    assert first["strength"] == "500 mg" and first["document_id"] == new and first["unverified"] == ["dose_instruction"]
    assert first["earlier_dates"] == ["2025-04-10"] and result["days"] == report.RECENT_DAYS
    assert result["undated"] == 1 and result["older"] == 1
    assert old and new


def test_the_medication_list_api_needs_no_setup_and_starts_empty():
    body = client.get("/api/dashboard/medications").json()
    assert body["medications"] == [] and body["days"] == report.RECENT_DAYS
    prescription(date.today() - timedelta(days=3), [{"name": "Δείγμα"}])
    assert [m["name"] for m in client.get("/api/dashboard/medications").json()["medications"]] == ["Δείγμα"]


# --- `other`: lab or report ---------------------------------------------------------------------------------


def test_choose_route_needs_lab_pages_to_be_half_of_the_pages_with_text():
    lab, prose = LAB_PAGE, NARRATIVE
    assert report.choose_route({1: lab, 2: lab, 3: prose}) == ("lab", 2, 3)
    assert report.choose_route({1: lab, 2: prose}) == ("lab", 1, 2)  # a tie stays what `other` always was
    assert report.choose_route({1: lab, 2: prose, 3: prose}) == ("report", 1, 3)
    assert report.choose_route({1: prose}) == ("report", 0, 1)
    assert report.choose_route({}) == ("lab", 0, 0) and report.choose_route({1: "  "}) == ("lab", 0, 0)


def test_an_other_document_of_prose_is_read_as_a_report(caplog):
    FakeOllama.page_texts = [NARRATIVE, NARRATIVE]
    FakeOllama.answer = {"conclusion": "A nodule.", "key_findings": ["Nodule 3.5 mm"]}
    doc_id = make_doc(DocumentKind.OTHER)
    with caplog.at_level("INFO", logger="vibehealth"):
        read(doc_id)
    assert FakeOllama.calls.count("text") == 2 and "rows" not in FakeOllama.calls and "glm" not in FakeOllama.calls
    assert FakeOllama.calls[-1][1] is GENERIC
    assert run_routes(doc_id) == ["auto:report"]
    assert stored(doc_id).conclusion == "A nodule." and details(doc_id) == {}
    assert any("read as report (0 of 2 pages" in r.getMessage() for r in caplog.records)  # the decision is logged
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["read_mode"] == "text" and doc["read_route"] == "auto:report" and doc["can_switch"] is True


def test_an_other_document_of_lab_pages_goes_through_the_lab_pipeline(caplog):
    FakeOllama.page_texts = [LAB_PAGE, LAB_PAGE, NARRATIVE]
    doc_id = make_doc(DocumentKind.OTHER)
    with caplog.at_level("INFO", logger="vibehealth"):
        read(doc_id)
    assert "rows" in FakeOllama.calls and "glm" in FakeOllama.calls and not any(isinstance(c, tuple) for c in FakeOllama.calls)
    assert run_routes(doc_id) == ["auto:lab"] and stored(doc_id) is None
    assert any("read as lab (2 of 3 pages" in r.getMessage() for r in caplog.records)
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["read_mode"] == "lab" and doc["read_route"] == "auto:lab" and doc["can_switch"] is True


def test_the_kinds_other_than_other_are_not_auto_detected():
    FakeOllama.page_texts = [LAB_PAGE, LAB_PAGE]
    blood, imaging = make_doc(DocumentKind.BLOOD_TEST), make_doc(DocumentKind.IMAGING)
    read(blood)
    assert "text" not in FakeOllama.calls and run_routes(blood) == ["kind:lab"]
    read(imaging)  # lab-looking pages in an imaging report stay a report (a notice offers the lab reading)
    assert run_routes(imaging) == ["kind:report"] and stored(imaging).lab_pages == "[1, 2]"
    assert client.get(f"/api/documents/{blood}").json()["can_switch"] is False


def test_switching_by_hand_is_kept_for_later_readings_and_keeps_approved_values():
    FakeOllama.page_texts = [NARRATIVE]
    FakeOllama.answer = {"conclusion": "A nodule.", "key_findings": []}
    doc_id = make_doc(DocumentKind.OTHER)
    with Session(engine) as s:
        s.add(ExtractedValue(document_id=doc_id, test_code="HGB", raw_name="Hemoglobin", value_text="14.8",
                             status=ValueStatus.APPROVED))
        s.add(ExtractedValue(document_id=doc_id, raw_name="junk", value_text="1", status=ValueStatus.NEEDS_REVIEW))
        s.commit()
    read(doc_id)  # auto: report
    assert stored(doc_id) is not None
    read(doc_id, "lab")  # "read as blood test": the report's rows go, the approved value stays
    assert stored(doc_id) is None
    with Session(engine) as s:
        assert s.exec(select(DocumentText)).all() == [] and s.exec(select(DocumentSearch)).all() == []
        assert [v.test_code for v in s.exec(select(ExtractedValue)).all() if v.status == ValueStatus.APPROVED] == ["HGB"]

    FakeOllama.calls = []
    read(doc_id)  # no force: the manual choice stands, no transcription
    assert "text" not in FakeOllama.calls and "rows" in FakeOllama.calls
    assert run_routes(doc_id) == ["auto:report", "manual:lab", "manual:lab"]
    assert client.get(f"/api/documents/{doc_id}").json()["read_mode"] == "lab"

    FakeOllama.calls = []
    read(doc_id, "report")  # "read as report"
    assert stored(doc_id).conclusion == "A nodule." and run_routes(doc_id)[-1] == "manual:report"
    assert client.get(f"/api/documents/{doc_id}").json()["read_mode"] == "text"
    with Session(engine) as s:  # approved lab values are kept, as before
        assert [v.test_code for v in s.exec(select(ExtractedValue)).all()] == ["HGB"]


def test_the_read_endpoint_takes_as_report_only_for_other_documents():
    other, imaging = make_doc(DocumentKind.OTHER), make_doc(DocumentKind.IMAGING)
    assert client.post(f"/api/documents/{imaging}/read", params={"as_report": "true"}).status_code == 400
    assert client.post(f"/api/documents/{other}/read", params={"as_report": "true", "as_lab": "true"}).status_code == 400
    assert client.post(f"/api/documents/{other}/read", params={"as_report": "true"}).json() == {"queued": True, "already": False}
    assert worker._force[other] == "report"


# --- re-reading and old summaries ---------------------------------------------------------------------------------


def test_reading_again_refreshes_the_fields_and_keeps_approved_values():
    FakeOllama.answer = IMAGING_ANSWER
    doc_id = make_doc(DocumentKind.IMAGING)
    with Session(engine) as s:
        s.add(ExtractedValue(document_id=doc_id, test_code="HGB", raw_name="Hemoglobin", value_text="14.8",
                             status=ValueStatus.APPROVED))
        s.commit()
    read(doc_id)
    assert details(doc_id)["modality"] == "ultrasound"
    FakeOllama.answer = {**IMAGING_ANSWER, "modality": "MRI", "measurements": []}
    read(doc_id)
    assert details(doc_id)["modality"] == "mri" and details(doc_id)["measurements"] == []
    with Session(engine) as s:
        assert [v.test_code for v in s.exec(select(ExtractedValue)).all()] == ["HGB"]


def test_a_summary_from_before_the_fields_existed_still_displays():
    doc_id = make_doc(DocumentKind.IMAGING, title="old imaging")
    damaged = make_doc(DocumentKind.REPORT, title="damaged")
    with Session(engine) as s:
        s.add(DocumentReport(document_id=doc_id, summary_status="ok", conclusion="Old conclusion.", key_findings='["a", "b"]'))
        s.add(DocumentText(document_id=doc_id, page=1, text="Παλιό κείμενο υπερήχου"))
        s.add(DocumentReport(document_id=damaged, summary_status="ok", conclusion="x", details="not json", key_findings="[1"))
        s.commit()
    body = client.get(f"/api/documents/{doc_id}/report").json()["report"]
    assert body["details"] == {} and body["conclusion"] == "Old conclusion." and body["key_findings"] == ["a", "b"]
    listed = {d["id"]: d for d in client.get("/api/documents").json()}
    assert listed[doc_id]["report"] == {"status": "ok", "findings": 2, "conclusion": "Old conclusion.",
                                        "modality": "", "regions": [], "diagnosis": "", "medications": 0}
    assert client.get(f"/api/documents/{damaged}/report").json()["report"]["details"] == {}
    # and it is searchable without any migration of its rows
    assert [r["document"]["id"] for r in client.get("/api/search", params={"q": "υπερηχου"}).json()["results"]] == [doc_id]


def test_the_report_api_and_the_list_carry_the_kind_fields():
    FakeOllama.answer = IMAGING_ANSWER
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)
    body = client.get(f"/api/documents/{doc_id}/report").json()["report"]
    assert body["details"]["modality"] == "ultrasound" and len(body["details"]["measurements"]) == 2
    brief = {d["id"]: d for d in client.get("/api/documents").json()}[doc_id]["report"]
    assert brief["modality"] == "ultrasound" and brief["regions"] == ["thyroid"] and brief["findings"] == 1

    FakeOllama.page_texts = [PRESCRIPTION]
    FakeOllama.answer = PRESCRIPTION_ANSWER
    rx = make_doc(DocumentKind.PRESCRIPTION)
    read(rx)
    assert {d["id"]: d for d in client.get("/api/documents").json()}[rx]["report"]["medications"] == 2


# --- search --------------------------------------------------------------------------------------------------------


def add_report(title, text, *, conclusion="", findings=(), meds=(), ignored=False, kind=DocumentKind.IMAGING, doc_date=None):
    doc_id = make_doc(kind, title=title, ignored=ignored, doc_date=doc_date)
    with Session(engine) as s:
        s.add(DocumentReport(document_id=doc_id, summary_status="ok", conclusion=conclusion,
                             key_findings=json.dumps(list(findings), ensure_ascii=False),
                             details=json.dumps({"medications": list(meds)}, ensure_ascii=False)))
        s.add(DocumentText(document_id=doc_id, page=1, text=text))
        s.commit()
        search.index_document(s, doc_id)
        s.commit()
    return doc_id


def found(q):
    body = client.get("/api/search", params={"q": q}).json()
    return [r["document"]["id"] for r in body["results"]]


def test_folding_ignores_case_accents_and_the_final_sigma_and_keeps_the_length():
    assert fold(nfc("Ήπαρ ΠΟΛΎΠΟΔΑΣ Πολύποδας")) == "ηπαρ πολυποδασ πολυποδασ"
    assert fold("Crème") == "creme" and len(fold("İ")) == 1
    text = nfc("Ο πολύποδας της χοληδόχου")
    assert len(fold(text)) == len(text)


def test_search_finds_greek_text_whatever_the_accents_and_case():
    doc_id = add_report("Υπέρηχος άνω κοιλίας", "Στο τοίχωμα της χοληδόχου κύστεως υπάρχει μικρός πολύποδας 3,5 mm.\nΗπαρ φυσιολογικό.",
                        conclusion="Πολύποδας χοληδόχου.")
    other = add_report("Άλλο", "Καρωτίδες: χωρίς στένωση.")
    for query in ("πολυποδας", "ΠΟΛΎΠΟΔΑΣ", "Πολύποδας", "πολυποδα", "ηπαρ", "χοληδοχου κυστεως"):
        assert found(query) == [doc_id], query
    assert found("καρωτιδες") == [other] and found("πολυποδας καρωτιδες") == []  # every word must be found
    assert found("δεν υπάρχει αυτό") == []
    hit = client.get("/api/search", params={"q": "ΠΟΛΥΠΟΔΑΣ"}).json()["results"][0]
    assert hit["document"]["id"] == doc_id and hit["document"]["title"] == "Υπέρηχος άνω κοιλίας"
    first = hit["snippets"][0]
    assert first["field"] == "summary" and first["match"] == "Πολύποδας"  # cut from the original: accents and case kept
    page = [s for s in hit["snippets"] if s["field"] == "text"][0]
    assert page["page"] == 1 and page["match"] == "πολύποδας" and "3,5 mm" in page["after"] and "χοληδόχου" in page["before"]


def test_search_covers_findings_measurements_diagnoses_and_medication_names():
    a = add_report("a", "x", findings=["Θυρεοειδής: όζος 4 mm"])
    with Session(engine) as s:
        rep = s.get(DocumentReport, a)
        rep.details = json.dumps({"measurements": [{"label": "διάμετρος όζου", "value": "4", "unit": "mm"}],
                                  "diagnoses": ["Οζώδης βρογχοκήλη"], "regions": ["θυρεοειδής"]}, ensure_ascii=False)
        s.add(rep)
        s.commit()
        search.index_document(s, a)
        s.commit()
    rx = add_report("rx", "y", kind=DocumentKind.PRESCRIPTION,
                    meds=[{"name": "ΠΑΡΑΔΕΙΓΜΑ", "active_substance": "Παρακεταμόλη", "strength": "500 mg"}])
    assert found("οζος") == [a] and found("βρογχοκηλη") == [a] and found("διαμετρος") == [a]
    assert found("παρακεταμολη") == [rx] and found("παραδειγμα") == [rx]
    snippet = client.get("/api/search", params={"q": "παρακεταμολη"}).json()["results"][0]["snippets"][0]
    assert snippet["field"] == "medication" and snippet["match"] == "Παρακεταμόλη"


def test_search_skips_hidden_documents_matches_titles_and_ignores_wildcards():
    add_report("hidden", "μυστικό εύρημα", ignored=True)
    visible = add_report("Τίτλος με λέξη Ζέβρα", "κάτι άλλο")
    assert found("μυστικο") == [] and found("ζεβρα") == [visible]
    assert client.get("/api/search", params={"q": "ζεβρα"}).json()["results"][0]["snippets"][0]["field"] == "title"
    assert found("%%") == [] and found("κατι_") == [] and found("κ%") == []
    assert found("a") == []  # too short to search for
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_a_reading_updates_the_search_text_and_a_deleted_document_leaves_none():
    FakeOllama.page_texts = ["Υπερηχογράφημα θυρεοειδούς: όζος"]
    FakeOllama.answer = {"conclusion": "Όζος.", "key_findings": []}
    doc_id = make_doc(DocumentKind.IMAGING)
    read(doc_id)
    assert found("θυρεοειδους") == [doc_id]
    FakeOllama.page_texts = ["Καρωτίδες φυσιολογικές"]
    FakeOllama.answer = {"conclusion": "Φυσιολογικό.", "key_findings": []}
    read(doc_id)
    assert found("θυρεοειδους") == [] and found("καρωτιδες") == [doc_id]


@pytest.mark.fresh_install
@pytest.mark.parametrize("path", ["/api/search?q=abc", "/api/dashboard/medications"])
def test_search_and_the_medication_list_need_a_session(path):
    settings_store.update("auth", {"password_hash": security.hash_password("x" * 12)})
    assert TestClient(app).get(path).status_code == 401


# --- migration 4 ---------------------------------------------------------------------------------------------------


def v3_database(tmp_path):
    """A database as version 3 left it, with one summary written by that version."""
    path = legacy_db(tmp_path)
    migrations.run(make_engine(path), MIGRATIONS[:3])
    assert version(path) == 3
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO document_reports (document_id, summary_status, summary_error, conclusion, key_findings, "
                  "auto_generated, summary_model, lab_pages, updated_at) VALUES (1, 'ok', '', 'Old.', '[\"a\"]', 1, 'm', '[]', "
                  "'2025-03-03 09:00:00')")
    return path


def test_migration_4_adds_two_columns_and_a_table_and_changes_no_row(tmp_path):
    path = v3_database(tmp_path)
    before = dump(path)
    with sqlite3.connect(path) as c:
        old_report = c.execute("SELECT * FROM document_reports").fetchall()

    result = migrations.run(make_engine(path))
    assert result["applied"] == [4] and version(path) == 4
    assert result["backup"] and "pre-v4-" in result["backup"]  # a database with data is copied first
    after = dump(path)
    assert after["documents"] == before["documents"] and after["extracted_values"] == before["extracted_values"]
    assert [r[:-1] for r in after["extraction_runs"]] == before["extraction_runs"]  # only the new column is added
    assert {r[-1] for r in after["extraction_runs"]} == {""}
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT * FROM document_reports").fetchall() == [(*old_report[0], "{}")]
        assert c.execute("SELECT count(*) FROM document_search").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert shape(path) == fresh_shape(tmp_path)  # what a fresh install has

    # the old summary is readable by the new code, and searchable
    with Session(make_engine(path)) as s:
        assert details_of_first(s) == {}


def details_of_first(session):
    return report.details_of(session.exec(select(DocumentReport)).first())


def test_migration_4_is_harmless_where_it_has_already_been_applied(tmp_path):
    path = str(tmp_path / "fresh.db")
    migrations.run(make_engine(path))
    before = shape(path)
    with sqlite3.connect(path) as c:
        migrations._report_details(c)
        migrations._report_details(c)
    assert shape(path) == before


def test_a_fresh_install_has_the_new_columns_and_the_latest_version(tmp_path):
    path = str(tmp_path / "new.db")
    assert migrations.run(make_engine(path))["fresh"] and version(path) == migrations.latest_version() == 4
    with sqlite3.connect(path) as c:
        assert "details" in [r[1] for r in c.execute("PRAGMA table_info(document_reports)")]
        assert "route" in [r[1] for r in c.execute("PRAGMA table_info(extraction_runs)")]
        assert c.execute("SELECT count(*) FROM document_search").fetchone()[0] == 0


# --- the Ollama call ---------------------------------------------------------------------------------------------


def test_the_summary_call_sends_the_spec_of_the_kind_and_returns_the_answer_unchecked(monkeypatch):
    import httpx

    real, seen = httpx.AsyncClient, []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done_reason": "stop", "message": {"content": '```json\n{"modality": "MRI", "x": 1}\n```'}})

    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    answer = asyncio.run(ollama.Ollama("http://ollama.test:11434/", 30, 8192, "5m").summarize("qwen3.5:4b", "[page 1]\ntext", IMAGING))
    assert answer == {"modality": "MRI", "x": 1}  # cleaning is report.clean's job
    body = seen[0]
    assert body["format"] is IMAGING.schema or body["format"] == IMAGING.schema
    assert body["messages"][0] == {"role": "system", "content": IMAGING.prompt} and body["messages"][1]["content"] == "[page 1]\ntext"
    assert body["options"]["num_predict"] == IMAGING.num_predict and body["think"] is False
