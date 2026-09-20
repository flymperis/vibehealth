"""Rendering, and the whole pipeline with Paperless and Ollama replaced by fakes."""

import asyncio
import io

from PIL import Image
from sqlmodel import Session, select

from app import reading
from app.db import engine
from app.models import Document, DocumentKind, ExtractedValue, ExtractionRun
from app.ollama import PageError, describe
from app.render import Pages


def synthetic_pdf(pages=2) -> bytes:
    images = [Image.new("RGB", (595, 842), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return buf.getvalue()


def test_render_pdf_and_image():
    pages = Pages(synthetic_pdf(2), "application/pdf")
    assert len(pages) == 2
    img = Image.open(io.BytesIO(pages.png(1, 144)))
    assert img.size == (1190, 1684)
    pages.close()

    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000)).save(buf, "JPEG")
    photo = Pages(buf.getvalue(), "image/jpeg")
    assert len(photo) == 1
    assert max(Image.open(io.BytesIO(photo.png(0, 150))).size) == 3000
    assert max(Image.open(io.BytesIO(photo.png(0, 100))).size) == 2666


def test_describe_never_empty_and_never_raw_library_text():
    assert describe(PageError("  ")) == "PageError"
    assert describe(PageError("answer cut off (length)")) == "answer cut off (length)"
    assert describe(TimeoutError()) == "Unexpected error (TimeoutError)"
    # a library message can carry an address or a path: only the class name is shown
    assert describe(ValueError("boom at http://198.51.100.5:8000/x")) == "Unexpected error (ValueError)"


class FakePaperless:
    async def download(self, paperless_id, original=True):
        return synthetic_pdf(2), "application/pdf"

    async def document(self, paperless_id):
        return {"content": "Κάλιο (K) 4.4 mmol/l 3.5 - 5.1\nΑντίδραση (pH) Όξινη 5.5"}


class FakeOllama:
    calls: list = []

    def __init__(self, *args):
        pass

    async def models(self):
        return ["qwen3.5:4b", "glm-ocr:latest"]

    async def read_rows(self, model, png):
        FakeOllama.calls.append(("A", len(png)))
        if len([c for c in FakeOllama.calls if c[0] == "A"]) == 1:
            return [
                {"name": "Ουρία", "value": "30", "unit": "mg/dl", "reference_range": "10 - 50"},
                {"name": "Κάλιο", "value": "4,4", "unit": "mmol/l", "reference_range": "3.5 - 5.1"},
                {"name": "Αντίδραση (pH)", "value": "Όξινη", "unit": "", "reference_range": ""},
            ]
        raise PageError("answer cut off (length)")

    async def read_text(self, model, png):
        FakeOllama.calls.append(("B", len(png)))
        n = len([c for c in FakeOllama.calls if c[0] == "B"])
        if n == 1:
            return "Ουρία ....... 30 mg/dl 10 - 50\nΚρεατινίνη ....... 0.9 mg/dl 0.7 - 1.3"
        if n == 2:  # page 2 at 150 dpi: cut short -> retried at 100 dpi
            return "Ουρία ....... 41 mg/dl\nΣάκχαρο . . . . . . ."
        return "Σάκχαρο ....... 95 mg/dl 70 - 110"


def test_pipeline_end_to_end(monkeypatch):
    monkeypatch.setattr(reading, "Paperless", FakePaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    FakeOllama.calls = []
    with Session(engine) as s:
        doc = Document(paperless_id=5, title="synthetic", kind=DocumentKind.BLOOD_TEST)
        s.add(doc)
        s.commit()
        doc_id = doc.id

    progress = {}
    summary = asyncio.run(reading.read_document(doc_id, progress))
    assert progress["stage"] == "verify" and progress["pages"] == 2
    assert summary["status"] == "done"
    assert [e["reader"] for e in summary["page_errors"]] == ["A"]
    # page 2 of reader B: 150 dpi truncated, 100 dpi fine
    assert [c[0] for c in FakeOllama.calls] == ["A", "A", "B", "B", "B"]
    assert FakeOllama.calls[4][1] < FakeOllama.calls[3][1]

    with Session(engine) as s:
        rows = {v.test_code: v for v in s.exec(select(ExtractedValue))}
        run = s.exec(select(ExtractionRun)).one()
    assert rows["UREA"].status == "verified" and rows["UREA"].page == 1  # both readers; page 2's 41 ignored
    assert rows["K"].status == "verified"  # reader A + Paperless text
    assert rows["U_PH"].status == "needs_review"  # the text line carries another number
    assert rows["CREA"].status == "needs_review"  # only reader B
    assert rows["GLU"].status == "needs_review" and rows["GLU"].page == 2
    assert run.verified == 2 and run.needs_review == 3 and run.duration_s is not None


def test_pipeline_reports_missing_model(monkeypatch):
    class NoModels(FakeOllama):
        async def models(self):
            return ["qwen3.5:4b"]

    monkeypatch.setattr(reading, "Paperless", FakePaperless)
    monkeypatch.setattr(reading, "Ollama", NoModels)
    with Session(engine) as s:
        doc = Document(paperless_id=6, title="synthetic")
        s.add(doc)
        s.commit()
        doc_id = doc.id
    try:
        asyncio.run(reading.read_document(doc_id, {}))
    except Exception as exc:  # noqa: BLE001
        assert "glm-ocr is not installed" in str(exc)
    else:
        raise AssertionError("expected an error")
    with Session(engine) as s:
        run = s.exec(select(ExtractionRun)).one()
    assert run.status == "error" and "glm-ocr" in run.error
