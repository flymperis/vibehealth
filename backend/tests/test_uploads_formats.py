"""Small format decisions: multi-picture JPEGs (MPO), PDFs with only an owner password, and the
decoders being restricted to the format the bytes announce."""

import io
import os

import pytest
from conftest import PasswordClient
from PIL import Image
from sqlmodel import Session
from upload_helpers import encrypted_pdf, photo

from app import sandbox, sandbox_child, uploads
from app.db import engine
from app.main import app
from app.models import Document

client = PasswordClient(app)
URL = "/api/documents/upload"


def upload(data: bytes, name: str, ctype="application/octet-stream"):
    return client.post(URL, files={"file": (name, data, ctype)})


def mpo(size=(300, 200), orientation=None) -> bytes:
    """A multi-picture JPEG, as many phones write them: the first picture is red, the second blue."""
    first, second = Image.new("RGB", size, "red"), Image.new("RGB", size, "blue")
    buf = io.BytesIO()
    save = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        save["exif"] = exif
    first.save(buf, "MPO", save_all=True, append_images=[second], **save)
    return buf.getvalue()


def test_the_test_file_really_is_an_mpo():
    data = mpo()
    assert data.startswith(b"\xff\xd8\xff") and uploads.sniff(data) == "jpeg"
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "MPO" and getattr(image, "n_frames", 1) == 2


def test_a_multi_picture_jpeg_is_accepted_as_a_jpeg(tmp_path):
    r = upload(mpo(), "IMG_0001.JPG", "image/jpeg")
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert doc["mime_type"] == "image/jpeg" and doc["original_filename"] == "IMG_0001.jpg"
    assert client.get(f"/api/documents/{doc['id']}/thumbnail").status_code == 200
    with Session(engine) as s:
        path = uploads.stored_file(s.get(Document, doc["id"]).stored_path)
    pages = sandbox.SandboxPages(path, "jpeg")
    first = Image.open(io.BytesIO(pages.png(0, 150)))
    assert len(pages) == 1 and first.getpixel((10, 10))[0] > 200  # the first picture (red), upright


def test_a_multi_picture_jpeg_with_an_exif_turn_is_read_upright():
    r = upload(mpo(size=(300, 200), orientation=6), "turned.jpg")
    assert r.status_code == 201
    with Session(engine) as s:
        path = uploads.stored_file(s.get(Document, r.json()["document"]["id"]).stored_path)
    assert Image.open(io.BytesIO(sandbox.SandboxPages(path, "jpeg").png(0, 150))).size == (200, 300)


def test_decoders_are_limited_to_the_announced_format(tmp_path):
    """Pillow is told which formats it may use: a file that is not that format is not decoded as
    something else, whatever else Pillow would recognise."""
    assert sandbox_child.IMAGE_FORMATS == {"jpeg": ["JPEG", "MPO"], "png": ["PNG"], "webp": ["WEBP"]}
    gif = io.BytesIO()
    Image.new("P", (10, 10)).save(gif, "GIF")
    path = tmp_path / "x.png"
    path.write_bytes(gif.getvalue())
    with pytest.raises(sandbox.Refused, match="damaged"):
        sandbox.run("check_upload", {"path": str(path), "kind": "png"})
    png = tmp_path / "real.png"
    png.write_bytes(photo("PNG"))
    with pytest.raises(sandbox.Refused):  # a real PNG, but asked as a JPEG
        sandbox.run("check_upload", {"path": str(png), "kind": "jpeg"})
    assert sandbox.run("check_upload", {"path": str(png), "kind": "png"}).meta["pages"] == 1


def test_a_pdf_with_only_an_owner_password_is_accepted_and_readable():
    """Protection against copying or printing (an owner password, empty user password) opens in every
    viewer without a password: it is readable, so it is accepted. A PDF that needs a password to open
    is still refused (test_uploads_api)."""
    data = encrypted_pdf("", "owner")
    r = upload(data, "restricted.pdf", "application/pdf")
    assert r.status_code == 201, r.text
    did = r.json()["document"]["id"]
    assert client.get(f"/api/documents/{did}/thumbnail").status_code == 200
    assert client.get(f"/api/documents/{did}/preview").content == data  # the original is kept as it came
    with Session(engine) as s:
        path = uploads.stored_file(s.get(Document, did).stored_path)
    pages = sandbox.SandboxPages(path, "pdf")
    assert len(pages) == 1 and pages.png(0, 150).startswith(b"\x89PNG")


def test_a_pdf_that_needs_a_password_is_still_refused():
    r = upload(encrypted_pdf("secret", "owner"), "locked.pdf", "application/pdf")
    assert r.status_code == 422 and "password" in r.json()["detail"]
    assert not os.listdir(uploads.tmp_root())
