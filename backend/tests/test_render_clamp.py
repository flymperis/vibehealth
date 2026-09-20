"""No page is drawn above 3500 px on its long side, for every source and every dpi the settings allow,
and nothing below that changes: A4 at 150 / 200 dpi comes out exactly as it did."""

import asyncio
import io

import pytest
from conftest import PasswordClient
from PIL import Image
from upload_helpers import pdf

from app import reading, render, sandbox
from app.main import app

client = PasswordClient(app)


def size_of(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


@pytest.mark.parametrize("dpi,expected", [(72, (595, 842)), (100, (827, 1170)), (150, (1240, 1755)), (200, (1653, 2339))])
def test_normal_a4_output_is_unchanged(dpi, expected):
    assert render.page_scale(595, 842, dpi) == dpi / 72  # not a hair different
    assert size_of(render.Pages(pdf(1), "application/pdf").png(0, dpi)) == expected


def test_the_pixels_of_a_normal_render_are_those_of_a_direct_pdfium_render():
    import pypdfium2 as pdfium

    data = pdf(1)
    doc = pdfium.PdfDocument(data)
    direct = doc[0].render(scale=150 / 72).to_pil().convert("RGB")
    ours = Image.open(io.BytesIO(render.Pages(data, "application/pdf").png(0, 150)))
    assert ours.size == direct.size and ours.tobytes() == direct.tobytes()
    doc.close()


@pytest.mark.parametrize("page,dpi,expected", [
    ((595, 842), 300, (2474, 3500)),  # A4 at the settings' upper bound: 3508 px would be 8 px over
    ((2000, 2000), 150, (3500, 3500)),  # the largest page an upload may have, at the default dpi
    ((2000, 2000), 300, (3500, 3500)),
    ((1500, 300), 200, (3500, 700)),  # a wide page: the long side is what counts
])
def test_a_page_is_never_drawn_above_3500_px(page, dpi, expected):
    data = pdf(1, size=page)
    got = size_of(render.Pages(data, "application/pdf").png(0, dpi))
    assert got == expected and max(got) <= render.MAX_RENDER_SIDE


def test_the_sandbox_clamps_the_same_way_and_draws_the_same_pixels(tmp_path):
    data = pdf(1, size=(2000, 2000))
    path = tmp_path / "big.pdf"
    path.write_bytes(data)
    via_sandbox = sandbox.SandboxPages(str(path), "pdf").png(0, 300)
    assert size_of(via_sandbox) == (3500, 3500)
    assert via_sandbox == render.Pages(data, "application/pdf").png(0, 300)


def test_the_setting_driven_300_dpi_reaches_the_readers_clamped(monkeypatch):
    from test_uploads_pipeline import FakeOllama, NoPaperless

    monkeypatch.setattr(reading, "Paperless", NoPaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    FakeOllama.sizes = []
    client.get("/api/status")
    assert client.put("/api/reading/settings", json={"dpi": 300}).status_code == 200
    did = client.post(
        "/api/documents/upload", files={"file": ("big.pdf", pdf(1, size=(1800, 1800)), "application/pdf")}
    ).json()["document"]["id"]
    summary = asyncio.run(reading.read_document(did, {}))
    assert summary["status"] == "done"
    sizes = {s for _reader, s in FakeOllama.sizes}
    assert sizes and all(max(s) <= render.MAX_RENDER_SIDE for s in sizes), sizes


def test_a_paperless_page_is_clamped_too():
    """The in-process path (Paperless documents) obeys the same ceiling."""
    pages = render.Pages(pdf(1, size=(1900, 900)), "application/pdf")
    assert max(size_of(pages.png(0, 300))) == 3500
    pages.close()


def test_a_photo_scan_keeps_its_own_rules():
    from upload_helpers import photo

    data = photo("PNG", size=(4000, 1000))
    assert size_of(render.Pages(data, "image/png").png(0, 300)) == (3000, 750)  # the 3000 px scan rule, as before
