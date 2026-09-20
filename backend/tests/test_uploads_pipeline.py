"""An uploaded PDF and an uploaded photo through the whole reading pipeline, with a fake Ollama.

No Paperless is involved: any call to it fails the test. An upload has no OCR text, so a value
is verified by the two readers agreeing, and by nothing else.
"""

import asyncio
import io

import pytest
from conftest import PasswordClient
from PIL import Image
from sqlmodel import Session, select
from upload_helpers import pdf, photo

from app import reading
from app.db import engine
from app.main import app
from app.models import Document, ExtractedValue, ExtractionRun

client = PasswordClient(app)


class NoPaperless:
    """Constructing it is fine (the pipeline does); using it is not."""

    def __init__(self, *a, **kw):
        pass

    async def download(self, *a, **kw):
        raise AssertionError("an upload must not be fetched from Paperless")

    async def document(self, *a, **kw):
        raise AssertionError("an upload has no Paperless text")


class FakeOllama:
    sizes: list[tuple[str, tuple[int, int]]] = []
    rows_a = [
        {"name": "Ουρία", "value": "30", "unit": "mg/dl", "reference_range": "10 - 50"},
        {"name": "Κάλιο", "value": "4,4", "unit": "mmol/l", "reference_range": "3.5 - 5.1"},
        {"name": "Σάκχαρο", "value": "95", "unit": "mg/dl", "reference_range": "70 - 110"},
    ]
    text_b = "Ουρία ....... 30 mg/dl 10 - 50\nΚάλιο ....... 4.5 mmol/l 3.5 - 5.1\nΚρεατινίνη ....... 0.9 mg/dl 0.7 - 1.3"

    def __init__(self, *args):
        pass

    async def models(self):
        return ["qwen3.5:4b", "glm-ocr:latest"]

    async def read_rows(self, model, png):
        FakeOllama.sizes.append(("A", Image.open(io.BytesIO(png)).size))
        return list(self.rows_a)

    async def read_text(self, model, png):
        FakeOllama.sizes.append(("B", Image.open(io.BytesIO(png)).size))
        return self.text_b


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(reading, "Paperless", NoPaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    FakeOllama.sizes = []


def upload(data, name, ctype, **fields):
    r = client.post("/api/documents/upload", files={"file": (name, data, ctype)}, data=fields)
    assert r.status_code == 201, r.text
    return r.json()["document"]["id"]


def read(document_id: int) -> dict:
    progress: dict = {}
    summary = asyncio.run(reading.read_document(document_id, progress))
    assert summary["status"] == "done"
    return {"summary": summary, "progress": progress}


def values(document_id: int) -> dict:
    with Session(engine) as s:
        return {v.test_code: v for v in s.exec(select(ExtractedValue).where(ExtractedValue.document_id == document_id))}


def check_two_reader_verification(rows: dict):
    # both readers agree on the urea: verified, with no text to lean on
    assert rows["UREA"].status == "verified" and rows["UREA"].reason == "both readers agree"
    # they differ on the potassium (4,4 against 4.5) and there is no text to settle it
    assert rows["K"].status == "needs_review" and rows["K"].reason == "readers differ"
    # only one reader saw each of these
    assert rows["GLU"].status == "needs_review" and "only reader A" in rows["GLU"].reason
    assert rows["CREA"].status == "needs_review" and "only reader B" in rows["CREA"].reason
    assert all("Paperless text" not in v.reason for v in rows.values())


def test_uploaded_pdf_is_read_in_full():
    did = upload(pdf(2), "labs.pdf", "application/pdf", kind="blood_test")
    result = read(did)
    assert result["progress"]["pages"] == 2 and result["summary"]["pages"] == 2
    assert [s[0] for s in FakeOllama.sizes] == ["A", "A", "B", "B"]
    assert all(1238 <= w <= 1242 and 1750 <= h <= 1756 for _r, (w, h) in FakeOllama.sizes)  # A4 at 150 dpi
    rows = values(did)
    check_two_reader_verification(rows)
    assert result["summary"]["verified"] == 1 and result["summary"]["needs_review"] == 3

    # and it shows up through the API like any document
    detail = client.get(f"/api/documents/{did}/values").json()
    assert detail["document"]["reading"]["state"] == "done" and len(detail["values"]) == 4
    assert client.get("/api/documents").json()[0]["reading"]["verified"] == 1


def test_uploaded_photo_is_read_upright_and_at_the_upload_size():
    # stored 3000 x 1500 sideways, with the EXIF flag that says "turn it a quarter": really 1500 x 3000
    did = upload(photo("JPEG", size=(3000, 1500), orientation=6), "phone.jpg", "image/jpeg", kind="blood_test")
    result = read(did)
    assert result["progress"]["pages"] == 1
    sizes = {size for _r, size in FakeOllama.sizes}
    assert sizes == {(1100, 2200)}  # portrait, longest side 2200: the same picture, upright and smaller
    check_two_reader_verification(values(did))


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_uploaded_png_and_webp_are_read(fmt):
    did = upload(photo(fmt, size=(800, 1000)), f"scan.{fmt.lower()}", f"image/{fmt.lower()}", kind="blood_test")
    read(did)
    assert {size for _r, size in FakeOllama.sizes} == {(800, 1000)}  # already small: never enlarged
    assert values(did)["UREA"].status == "verified"


def test_reading_again_replaces_unapproved_values_and_keeps_approved_ones():
    did = upload(pdf(1), "a.pdf", "application/pdf", kind="blood_test")
    read(did)
    urea = values(did)["UREA"]
    assert client.post(f"/api/values/{urea.id}/approve").status_code == 200
    read(did)
    rows = values(did)
    assert rows["UREA"].status == "approved" and rows["UREA"].id == urea.id
    with Session(engine) as s:
        assert len(s.exec(select(ExtractionRun).where(ExtractionRun.document_id == did)).all()) == 2


def test_a_missing_file_fails_the_reading_with_a_clear_message():
    import os

    from app import uploads

    did = upload(pdf(1), "a.pdf", "application/pdf")
    with Session(engine) as s:
        os.remove(uploads.stored_file(s.get(Document, did).stored_path))
    with pytest.raises(Exception, match="missing from the data folder"):
        asyncio.run(reading.read_document(did, {}))
    with Session(engine) as s:
        run = s.exec(select(ExtractionRun).where(ExtractionRun.document_id == did)).one()
    assert run.status == "error" and "missing from the data folder" in run.error


def test_the_read_endpoint_and_the_queue_work_for_uploads():
    did = upload(pdf(1), "a.pdf", "application/pdf")
    assert client.post(f"/api/documents/{did}/read").json() == {"queued": True, "already": False}
    assert client.post(f"/api/documents/{did}/read").json() == {"queued": False, "already": True}
    assert client.get(f"/api/documents/{did}").json()["reading"]["state"] == "queued"
