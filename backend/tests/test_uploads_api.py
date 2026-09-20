"""POST /api/documents/upload, PATCH and DELETE, and the preview / thumbnail routes for uploads."""

import os
import re

import pytest
from conftest import PasswordClient
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from upload_helpers import animated_webp, encrypted_pdf, header_only_png, multipart, pdf, photo

from app import settings_store, uploads, worker
from app.db import engine
from app.main import app
from app.models import Document, DocumentSource, ExtractedValue, ExtractionRun, ValueStatus

client = PasswordClient(app)
URL = "/api/documents/upload"
STORED = re.compile(r"[0-9a-f]{2}/[0-9a-f]{32}\.(pdf|jpg|png|webp)")


def upload(data: bytes, name: str = "report.pdf", ctype: str = "application/pdf", c=client, **fields):
    return c.post(URL, files={"file": (name, data, ctype)}, data=fields)


def upload_raw(filename, data: bytes, c=client, ctype="application/pdf", **fields):
    body, headers = multipart(filename, data, ctype, fields)
    return c.post(URL, content=body, headers=headers)


def files_on_disk() -> list[str]:
    found = []
    for base, _dirs, names in os.walk(uploads.root()):
        found += [os.path.join(base, n) for n in names]
    return found


def stored_files() -> list[str]:
    return [f for f in files_on_disk() if os.sep + ".tmp" + os.sep not in f]


def doc_row(document_id: int) -> Document | None:
    with Session(engine) as s:
        return s.get(Document, document_id)


def count(model) -> int:
    with Session(engine) as s:
        return len(s.exec(select(model)).all())


# --- the happy path and the shape of the answer -----------------------------------------------------


def test_upload_pdf_answer_and_row():
    r = upload(pdf(2), "Blood work.pdf", kind="blood_test", title="March labs", doc_date="2025-03-01")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["read_queued"] is False
    doc = body["document"]
    assert doc["source"] == "upload" and doc["paperless_id"] is None and doc["paperless_link"] is None
    assert doc["title"] == "March labs" and doc["kind"] == "blood_test" and doc["doc_date"] == "2025-03-01"
    assert doc["original_filename"] == "Blood work.pdf" and doc["mime_type"] == "application/pdf"
    assert doc["has_file"] is True and doc["size_bytes"] == len(pdf(2)) and doc["ignored"] is False
    assert doc["reading"]["state"] == "not_read"
    row = doc_row(doc["id"])
    assert row.source == DocumentSource.UPLOAD and STORED.fullmatch(row.stored_path)
    assert re.fullmatch(r"[0-9a-f]{64}", row.sha256)
    assert len(stored_files()) == 1 and uploads.tmp_root() and os.listdir(uploads.tmp_root()) == []


def test_defaults_title_from_the_name_kind_other_no_date():
    doc = upload(photo("PNG"), "lab photo.png", "image/png").json()["document"]
    assert doc["title"] == "lab photo" and doc["kind"] == "other" and doc["doc_date"] is None
    assert doc["mime_type"] == "image/png"


def test_all_four_types_are_accepted():
    for data, name in ((pdf(1), "a.pdf"), (photo("JPEG"), "b.jpg"), (photo("PNG"), "c.png"), (photo("WEBP"), "d.webp")):
        assert upload(data, name).status_code == 201, name
    assert sorted(f.rsplit(".", 1)[1] for f in stored_files()) == ["jpg", "pdf", "png", "webp"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_files_and_folders_are_private():
    upload(pdf(1))
    path = stored_files()[0]
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    for folder in (uploads.root(), os.path.dirname(path), uploads.tmp_root(), uploads.thumbs_root()):
        assert oct(os.stat(folder).st_mode & 0o777) == "0o700"


def test_read_now_queues_a_reading():
    body = upload(pdf(1), read_now="true").json()
    assert body["read_queued"] is True
    assert worker.state["reading"]["queue"] == [body["document"]["id"]]


def test_unknown_form_fields_are_ignored():
    assert upload(pdf(1), whatever="x", ignored="y").status_code == 201


# --- refusals before anything is read -----------------------------------------------------------------


def test_refused_in_open_mode_with_a_clear_message():
    open_client = TestClient(app)
    r = upload(pdf(1), c=open_client)
    assert r.status_code == 403 and "Set a password first" in r.json()["detail"]
    assert files_on_disk() == [] and count(Document) == 0


def test_refused_when_uploads_are_turned_off():
    client.get("/api/status")  # signs in
    settings_store.update("uploads", {"enabled": False})
    r = upload(pdf(1))
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    assert count(Document) == 0


def test_login_is_needed_once_a_password_is_set():
    client.get("/api/status")
    assert upload(pdf(1), c=TestClient(app)).status_code == 401


def test_origin_and_csrf_checks_apply_to_uploads():
    client.get("/api/status")
    good = client.post(URL, files={"file": ("a.pdf", pdf(1), "application/pdf")},
                       headers={"Origin": "http://testserver"})
    assert good.status_code == 201
    other = pdf(2)
    for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        r = client.post(URL, files={"file": ("b.pdf", other, "application/pdf")}, headers=headers)
        assert r.status_code == 403, headers
    assert count(Document) == 1 and len(stored_files()) == 1


def test_not_multipart_and_missing_file_and_empty_file():
    client.get("/api/status")
    assert client.post(URL, json={"a": 1}).status_code == 415
    body, headers = multipart(None, b"", fields={"title": "x"})
    assert client.post(URL, content=body, headers=headers).status_code == 422  # no file part
    assert upload_raw("empty.pdf", b"").status_code == 422
    assert files_on_disk() == []


def test_two_files_are_refused_and_nothing_is_kept():
    b = "vhtestboundary7d3f"
    part = lambda data: (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n\r\n").encode() + data + b"\r\n"
    body = part(pdf(1)) + part(pdf(2)) + f"--{b}--\r\n".encode()
    r = client.post(URL, content=body, headers={"content-type": f"multipart/form-data; boundary={b}"})
    assert r.status_code == 422 and files_on_disk() == [] and count(Document) == 0


def test_bad_fields_are_refused():
    for fields in ({"kind": "nonsense"}, {"doc_date": "yesterday"}, {"doc_date": "1800-01-01"},
                   {"doc_date": "2999-01-01"}, {"read_now": "maybe"}, {"title": "x" * 5000}):
        r = upload(pdf(1), **fields)
        assert r.status_code == 422, fields
    assert files_on_disk() == [] and count(Document) == 0


# --- what the bytes are, not what the client says --------------------------------------------------


@pytest.mark.parametrize("payload,name,ctype", [
    (b"<html><script>alert(1)</script></html>", "report.pdf", "application/pdf"),
    (b"<script>alert(document.cookie)</script>", "photo.jpg", "image/jpeg"),
    (b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200, "scan.png", "image/png"),
    (b"#!/bin/sh\nrm -rf /\n", "x.pdf", "application/pdf"),
    (b"GIF89a" + b"\x00" * 50, "x.png", "image/png"),
    (b"PK\x03\x04" + b"\x00" * 50, "x.pdf", "application/pdf"),
])
def test_spoofed_types_are_refused_by_their_bytes(payload, name, ctype):
    r = upload(payload, name, ctype)
    assert r.status_code == 415, r.text
    assert files_on_disk() == [] and count(Document) == 0


def test_client_type_and_name_do_not_decide():
    r = upload(pdf(1), "payload.exe", "text/html")
    doc = r.json()["document"]
    assert r.status_code == 201
    assert doc["mime_type"] == "application/pdf" and doc["original_filename"] == "payload.pdf"
    assert stored_files()[0].endswith(".pdf")
    # and the other way round: a JPEG called .pdf is a JPEG
    doc2 = upload(photo("JPEG"), "really.pdf", "application/pdf").json()["document"]
    assert doc2["mime_type"] == "image/jpeg" and doc2["original_filename"] == "really.jpg"


def test_a_webp_riff_header_alone_is_not_enough():
    assert upload(b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x00" * 30, "x.webp", "image/webp").status_code == 415


# --- names ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,shown", [
    (b"../../x.pdf", "x.pdf"),
    (b"/etc/passwd", "passwd.pdf"),
    (b"C:\\Windows\\System32\\evil.pdf", "evil.pdf"),
    (b"..\\..\\y.pdf", "y.pdf"),
    (b"a\x00b.pdf", "ab.pdf"),
    (b"CON.pdf", "_CON.pdf"),
    (b"nul", "_nul.pdf"),
    (b"COM1.txt", "_COM1.pdf"),
    (b"..", "upload.pdf"),
    (b"   ", "upload.pdf"),
    ("re\u202eport.pdf".encode(), "report.pdf"),
    ("Αίμα ώρα.pdf".encode(), "Αίμα ώρα.pdf"),
])
def test_file_names_are_cleaned_and_never_become_paths(raw, shown):
    before = set(os.listdir(os.path.dirname(uploads.root())))
    r = upload_raw(raw, pdf(1))
    assert r.status_code == 201, (raw, r.text)
    doc = r.json()["document"]
    assert doc["original_filename"] == shown
    row = doc_row(doc["id"])
    assert STORED.fullmatch(row.stored_path)
    assert os.path.commonpath([os.path.realpath(uploads.root()), os.path.realpath(stored_files()[0])]) == \
        os.path.realpath(uploads.root())
    # nothing new next to uploads/ (the thumbnail cache is ours, and is made with the upload)
    assert set(os.listdir(os.path.dirname(uploads.root()))) - {"cache"} == before - {"cache"}


def test_a_very_long_name_is_cut():
    r = upload_raw(("x" * 3000 + ".pdf").encode(), pdf(1))
    assert r.status_code == 201
    assert len(r.json()["document"]["original_filename"]) <= 104 and len(r.json()["document"]["title"]) <= 100


# --- size ------------------------------------------------------------------------------------------


def test_the_size_limit_stops_the_stream_and_removes_the_temp_file():
    settings_store.update("uploads", {"max_file_mb": 1})
    # inside the route's body limit (cap + 64 KB of wrapping) but over the cap: the counter stops it
    data = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10 * 1024)
    r = upload(data)
    assert r.status_code == 413 and "limit of 1 MB" in r.json()["detail"]
    assert files_on_disk() == [] and count(Document) == 0


def test_a_body_far_over_the_limit_is_stopped_by_the_middleware():
    settings_store.update("uploads", {"max_file_mb": 1})
    client.get("/api/status")
    r = upload(b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 300 * 1024))
    assert r.status_code == 413
    assert files_on_disk() == [] and os.listdir(uploads.tmp_root()) == []


def test_a_chunked_body_with_no_length_is_counted_as_it_arrives():
    settings_store.update("uploads", {"max_file_mb": 1})
    client.get("/api/status")
    body, headers = multipart("big.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 300 * 1024))

    def chunks():
        for i in range(0, len(body), 65536):
            yield body[i:i + 65536]

    r = client.post(URL, content=chunks(), headers=headers)  # no Content-Length: chunked
    assert r.status_code == 413
    assert files_on_disk() == [] and os.listdir(uploads.tmp_root()) == [] and count(Document) == 0


def test_the_limit_follows_the_setting_and_the_setting_is_checked():
    settings_store.update("uploads", {"max_file_mb": 1})
    data = pdf(1)
    assert len(data) < 1024 * 1024 and upload(data).status_code == 201
    for bad in (0, 201, -5):
        assert client.put("/api/settings/uploads", json={"max_file_mb": bad}).status_code == 422
    assert client.put("/api/settings/uploads", json={"max_file_mb": 200}).status_code == 200
    assert client.get("/api/settings/uploads").json()["values"]["max_file_mb"] == 200


def test_body_limit_of_other_routes_is_unchanged():
    client.get("/api/status")
    assert client.put("/api/settings/uploads", content=b"x" * (1024 * 1024 + 10),
                      headers={"content-type": "application/json"}).status_code == 413


# --- duplicates ----------------------------------------------------------------------------------------


def test_the_same_file_twice_is_a_409_with_the_first_id_and_is_stored_once():
    first = upload(pdf(3), "one.pdf").json()["document"]["id"]
    r = upload(pdf(3), "two.pdf", title="other title")
    assert r.status_code == 409
    assert r.json() == {"detail": "This file has already been uploaded.", "document_id": first}
    assert len(stored_files()) == 1 and count(Document) == 1 and os.listdir(uploads.tmp_root()) == []


def test_a_different_file_is_not_a_duplicate():
    assert upload(pdf(1)).status_code == 201 and upload(pdf(2)).status_code == 201
    assert len(stored_files()) == 2


# --- files that do not open -------------------------------------------------------------------------------


@pytest.mark.parametrize("make,expect", [
    (lambda: encrypted_pdf("secret", "owner"), "password"),
    (lambda: pdf(41), "41 pages"),
    (lambda: pdf(1, size=(3000, 3000)), "unusable size"),
    (lambda: b"%PDF-1.4\nthis is not a pdf at all", "damaged"),
    (lambda: pdf(2)[: len(pdf(2)) // 2], "damaged"),
])
def test_pdfs_that_are_unusable_are_refused(make, expect):
    r = upload(make())
    assert r.status_code == 422, r.text
    assert expect in r.json()["detail"]
    assert files_on_disk() == [] and count(Document) == 0


def test_a_pdf_at_the_page_limit_is_accepted():
    assert upload(pdf(40)).status_code == 201


@pytest.mark.parametrize("data,name", [
    (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "a.jpg"),
    (photo("JPEG")[:200], "b.jpg"),
    (b"\x89PNG\r\n\x1a\n" + b"junk" * 20, "c.png"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 40, "d.webp"),
])
def test_images_that_do_not_open_are_refused(data, name):
    r = upload(data, name)
    assert r.status_code == 422 and "damaged" in r.json()["detail"], r.text
    assert files_on_disk() == []


@pytest.mark.parametrize("size", [(30000, 30000), (10000, 10000), (7000, 7000), (20000, 10), (1, 40000)])
def test_decompression_bombs_and_absurd_dimensions_are_refused_before_decoding(size):
    r = upload(header_only_png(*size), "big.png")
    assert r.status_code == 422 and "too large" in r.json()["detail"], (size, r.text)
    assert files_on_disk() == [] and count(Document) == 0


def test_a_large_but_normal_photo_is_accepted():
    assert upload(photo("JPEG", size=(6000, 4000)), "phone.jpg").status_code == 201


def test_animated_webp_is_accepted():
    r = upload(animated_webp(), "anim.webp")
    assert r.status_code == 201 and r.json()["document"]["mime_type"] == "image/webp"


def test_a_png_that_is_really_a_jpeg_is_refused():
    r = upload(b"\x89PNG\r\n\x1a\n" + photo("JPEG"), "x.png")
    assert r.status_code == 422


# --- PATCH -----------------------------------------------------------------------------------------------


def make_upload(**fields) -> int:
    return upload(pdf(1), **fields).json()["document"]["id"]


def test_patch_changes_title_kind_and_date():
    did = make_upload()
    r = client.patch(f"/api/documents/{did}", json={"title": "  New   title ", "kind": "report", "doc_date": "2024-12-31"})
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["kind"], r.json()["doc_date"]) == ("New title", "report", "2024-12-31")
    assert doc_row(did).updated_at >= doc_row(did).created_at
    # only what is sent changes, and a null date clears it
    r = client.patch(f"/api/documents/{did}", json={"doc_date": None})
    assert (r.json()["title"], r.json()["kind"], r.json()["doc_date"]) == ("New title", "report", None)
    assert client.patch(f"/api/documents/{did}", json={}).status_code == 200


@pytest.mark.parametrize("body", [
    {"title": ""}, {"title": "   "}, {"title": "x" * 201}, {"title": "x" * 5000}, {"title": None},
    {"kind": "nonsense"}, {"kind": None}, {"doc_date": "not a date"}, {"doc_date": "1899-12-31"},
    {"doc_date": "2999-01-01"}, {"paperless_id": 5}, {"stored_path": "../../x"}, {"source": "paperless"},
    {"sha256": "0" * 64}, {"ignored": True},
])
def test_patch_validation(body):
    did = make_upload(title="Original")
    r = client.patch(f"/api/documents/{did}", json=body)
    assert r.status_code == 422, (body, r.text)
    assert doc_row(did).title == "Original"


def test_patch_drops_control_characters_from_the_title():
    did = make_upload()
    r = client.patch(f"/api/documents/{did}", json={"title": "Lab\x00 results\u202e\n"})
    assert r.status_code == 200 and r.json()["title"] == "Lab results"


def test_patch_and_delete_of_missing_documents_are_404():
    assert client.patch("/api/documents/999", json={"title": "x"}).status_code == 404
    assert client.delete("/api/documents/999").status_code == 404


def paperless_doc() -> int:
    with Session(engine) as s:
        doc = Document(paperless_id=42, title="From Paperless")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc.id


def test_paperless_documents_can_be_neither_patched_nor_deleted():
    did = paperless_doc()
    r = client.patch(f"/api/documents/{did}", json={"title": "x"})
    assert r.status_code == 409 and "Paperless" in r.json()["detail"]
    r = client.delete(f"/api/documents/{did}")
    assert r.status_code == 409 and "Hide it instead" in r.json()["detail"]
    row = doc_row(did)
    assert row is not None and row.title == "From Paperless"
    # hiding works, as before
    assert client.post(f"/api/documents/{did}/ignore").json()["ignored"] is True


# --- DELETE ------------------------------------------------------------------------------------------------


def add_values(did: int) -> None:
    with Session(engine) as s:
        run = ExtractionRun(document_id=did)
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(ExtractedValue(document_id=did, run_id=run.id, test_code="HGB", value_text="13", status=ValueStatus.APPROVED))
        s.add(ExtractedValue(document_id=did, run_id=run.id, test_code="K", value_text="4", status=ValueStatus.VERIFIED))
        s.commit()


def test_delete_removes_rows_file_thumbnail_and_cache():
    keep = make_upload()
    other = upload(pdf(2)).json()["document"]["id"]
    add_values(keep)
    add_values(other)
    thumb = client.get(f"/api/documents/{other}/thumbnail")
    assert thumb.status_code == 200
    row = doc_row(other)
    original = uploads.stored_file(row.stored_path)
    cached = os.path.join(uploads.thumbs_root(), f"{row.sha256}.jpg")
    assert os.path.isfile(original) and os.path.isfile(cached)

    r = client.delete(f"/api/documents/{other}")
    assert r.status_code == 200 and r.json() == {"deleted": True, "file_removed": True}
    assert doc_row(other) is None
    assert not os.path.exists(original) and not os.path.exists(cached)
    with Session(engine) as s:
        assert [v.document_id for v in s.exec(select(ExtractedValue))] == [keep, keep]
        assert [r.document_id for r in s.exec(select(ExtractionRun))] == [keep]
    assert client.get(f"/api/documents/{other}").status_code == 404
    # the other upload is untouched, and the same file can be uploaded again after a delete
    assert os.path.isfile(uploads.stored_file(doc_row(keep).stored_path))
    assert upload(pdf(2)).status_code == 201


def test_delete_survives_a_file_that_cannot_be_removed(monkeypatch):
    did = make_upload()
    row = doc_row(did)
    original = uploads.stored_file(row.stored_path)
    real_remove = os.remove

    def stuck(path, *a, **kw):
        if os.path.realpath(path) == original:
            raise PermissionError("in use")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", stuck)
    r = client.delete(f"/api/documents/{did}")
    assert r.status_code == 200 and r.json() == {"deleted": True, "file_removed": False}
    assert doc_row(did) is None and os.path.isfile(original)  # the row is gone, the file waits
    with open(os.path.join(uploads.root(), ".pending-delete"), encoding="utf-8") as f:
        assert f.read().split() == [row.stored_path]

    monkeypatch.setattr(os, "remove", real_remove)
    result = uploads.sweep_at_start()  # the next start
    assert result["pending"] == 1 and not os.path.exists(original)
    assert not os.path.exists(os.path.join(uploads.root(), ".pending-delete"))


def test_a_document_that_is_being_read_or_queued_cannot_be_deleted():
    did = make_upload()
    worker.state["reading"]["queue"].append(did)
    r = client.delete(f"/api/documents/{did}")
    assert r.status_code == 409 and "being read" in r.json()["detail"]
    worker.state["reading"]["queue"].clear()
    worker.state["reading"]["current"] = {"document_id": did, "stage": "reader_a", "page": 1, "pages": 2}
    assert client.delete(f"/api/documents/{did}").status_code == 409
    assert doc_row(did) is not None and len(stored_files()) == 1
    worker.state["reading"]["current"] = None
    assert client.delete(f"/api/documents/{did}").status_code == 200


def test_a_document_being_deleted_cannot_be_queued_and_a_queued_delete_skips_the_reading():
    did = make_upload()
    assert worker.begin_delete(did) is True
    assert worker.request_read([did]) == []
    assert worker.begin_delete(did) is True  # not busy: a second claim is harmless
    import asyncio

    last_run = worker.state["reading"]["last_run"]
    asyncio.run(worker.read_one(did))  # popped from the queue a moment too late: skipped, no crash
    assert worker.state["reading"]["current"] is None and worker.state["reading"]["last_run"] is last_run
    with Session(engine) as s:
        assert s.exec(select(ExtractionRun).where(ExtractionRun.document_id == did)).all() == []  # never started
    worker.end_delete(did)
    assert worker.request_read([did]) == [did]


# --- showing an upload ---------------------------------------------------------------------------------


def test_preview_serves_the_stored_type_with_safe_headers_and_no_file_name():
    did = upload(pdf(1), "secret name.html", "text/html").json()["document"]["id"]
    r = client.get(f"/api/documents/{did}/preview")
    assert r.status_code == 200 and r.content == pdf(1)
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-disposition"] == "inline"
    assert "secret" not in str(dict(r.headers))

    jid = upload(photo("JPEG"), "x.png", "image/png").json()["document"]["id"]
    assert client.get(f"/api/documents/{jid}/preview").headers["content-type"] == "image/jpeg"


def test_thumbnails_are_made_on_demand_and_cached():
    pdf_id = upload(pdf(2), "a.pdf").json()["document"]["id"]
    img_id = upload(photo("JPEG", size=(2000, 1000), orientation=6), "b.jpg").json()["document"]["id"]
    from io import BytesIO

    from PIL import Image

    for did, expected in ((pdf_id, (283, 400)), (img_id, (200, 400))):
        r = client.get(f"/api/documents/{did}/thumbnail")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "private, no-store"
        size = Image.open(BytesIO(r.content)).size
        assert max(size) == 400 and abs(size[0] - expected[0]) <= 1 and abs(size[1] - expected[1]) <= 1, size
        assert os.path.isfile(os.path.join(uploads.thumbs_root(), f"{doc_row(did).sha256}.jpg"))
    # the second request is answered from the cache: even with the tools removed it works
    first = client.get(f"/api/documents/{pdf_id}/thumbnail").content
    assert client.get(f"/api/documents/{pdf_id}/thumbnail").content == first


def test_a_missing_file_is_a_404_and_has_file_false():
    did = make_upload()
    os.remove(uploads.stored_file(doc_row(did).stored_path))
    assert client.get(f"/api/documents/{did}/preview").status_code == 404
    assert client.get(f"/api/documents/{did}/thumbnail").status_code == 404
    assert client.get(f"/api/documents/{did}").json()["has_file"] is False


def test_a_stored_path_can_never_leave_the_uploads_folder():
    with Session(engine) as s:
        doc = Document(source="upload", title="tampered", stored_path="../../vibehealth.db", mime_type="application/pdf")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
    assert client.get(f"/api/documents/{did}/preview").status_code == 404
    assert client.get(f"/api/documents/{did}/thumbnail").status_code == 404
    assert client.delete(f"/api/documents/{did}").status_code == 200
    assert os.path.isfile(os.path.join(os.path.dirname(uploads.root()), "vibehealth.db"))


# --- the rest of the app knows about uploads --------------------------------------------------------------


def test_uploads_appear_in_lists_dashboard_examinations_and_values():
    did = upload(pdf(1), "lab.pdf", kind="blood_test", doc_date="2025-03-01").json()["document"]["id"]
    rid = upload(pdf(2), "rep.pdf", kind="report", doc_date="2025-04-01").json()["document"]["id"]
    pid = paperless_doc()
    with Session(engine) as s:
        s.add(ExtractedValue(document_id=did, test_code="HGB", value_text="19", flag="H", unit="g/dL",
                             ref_range="12-16", status=ValueStatus.APPROVED))
        s.commit()

    listing = client.get("/api/documents").json()
    assert {d["id"]: d["source"] for d in listing} == {did: "upload", rid: "upload", pid: "paperless"}
    paperless_entry = next(d for d in listing if d["id"] == pid)
    assert paperless_entry["paperless_id"] == 42 and paperless_entry["has_file"] is True
    assert paperless_entry["original_filename"] is None and paperless_entry["paperless_link"].endswith("/documents/42/details")

    summary = client.get("/api/dashboard/summary").json()
    assert {d["id"] for d in summary["recent_documents"]} == {did, rid, pid}
    assert summary["flagged_values"][0]["document_id"] == did and summary["has_any_approved"] is True
    assert {c["kind"]: c["count"] for c in summary["category_counts"]} == {"blood_test": 1, "report": 1, "other": 1}

    exams = client.get("/api/examinations", params={"kind": "report"}).json()
    assert [d["id"] for d in exams["documents"]] == [rid] and exams["documents"][0]["paperless_id"] is None
    blood = client.get("/api/examinations", params={"kind": "blood_test"}).json()
    assert blood["categories"][0]["tests"][0]["document_id"] == did
    by_test = client.get("/api/values/by-test").json()
    assert by_test[0]["latest"]["document_id"] == did

    detail = client.get(f"/api/documents/{did}/values").json()
    assert detail["document"]["source"] == "upload" and len(detail["values"]) == 1
    assert client.get("/api/reading/status").status_code == 200
    assert client.get("/api/status").json()["documents"] == {"total": 3, "ignored": 0}


def test_status_reports_uploads():
    open_status = TestClient(app).get("/api/status").json()["uploads"]
    base = {"max_total_mb": 10240, "total_mb_used": 0.0}
    assert open_status == {"enabled": True, "max_mb": 50, "password_required": True, **base}
    assert client.get("/api/status").json()["uploads"] == {
        "enabled": True, "max_mb": 50, "password_required": False, **base}
    settings_store.update("uploads", {"enabled": False, "max_file_mb": 20, "max_total_mb": 500})
    assert client.get("/api/status").json()["uploads"] == {
        "enabled": False, "max_mb": 20, "password_required": False, "max_total_mb": 500, "total_mb_used": 0.0}


def test_sync_never_touches_uploads(monkeypatch):
    import asyncio

    from fake_paperless import BASE, TOKEN, FakePaperless

    from app import services

    up = upload(pdf(1), "mine.pdf", kind="blood_test", title="My upload", doc_date="2025-03-01").json()["document"]
    before = doc_row(up["id"])
    FakePaperless().standard().install(monkeypatch)
    assert client.put("/api/settings/paperless", json={"url": BASE, "token": TOKEN}).status_code == 200
    result = asyncio.run(services.sync_documents())
    assert result["created"] == 3
    after = doc_row(up["id"])
    assert (after.title, after.kind, after.doc_date, after.updated_at, after.stored_path, after.sha256) == \
        (before.title, before.kind, before.doc_date, before.updated_at, before.stored_path, before.sha256)
    assert count(Document) == 4
    with Session(engine) as s:
        assert {d.source for d in s.exec(select(Document)) if d.id != up["id"]} == {"paperless"}
    # a second sync is a no-op for it too
    assert asyncio.run(services.sync_documents())["created"] == 0
    assert doc_row(up["id"]).title == "My upload"
