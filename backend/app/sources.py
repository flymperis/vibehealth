"""Where a document's file comes from: Paperless, or the upload folder.

Everything that needs the file (reading it, showing a preview or thumbnail, linking to it) asks
here, and the answer depends on `Document.source`. Callers pass a `Document` or a `DocRef` (a
snapshot that stays usable after its database session is closed).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from . import render, sandbox, uploads
from .models import Document, DocumentSource
from .ollama import SafeError
from .paperless import Paperless


log = logging.getLogger("vibehealth")


class SourceError(SafeError):
    pass


async def _from_paperless(fetch: Awaitable):
    """Await a Paperless fetch made for a browser (preview, thumbnail). Whatever goes wrong is a clean
    404 / 502 with a fixed message: never a 500, and never the exception's text, an address or a token
    (the log gets the class and the HTTP status only)."""
    try:
        return await fetch
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log.warning("Paperless file request failed: HTTP %s", code)
        if code == 404:
            raise HTTPException(404, "This file is not available in Paperless.") from None
        raise HTTPException(502, "Paperless could not provide this file right now.") from None
    except Exception as exc:  # noqa: BLE001 - timeouts, refused connections, a bad address, anything
        log.warning("Paperless file request failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Paperless could not provide this file right now.") from None


@dataclass(frozen=True)
class DocRef:
    id: int
    source: str
    paperless_id: int | None
    stored_path: str | None
    mime_type: str | None
    sha256: str | None

    @classmethod
    def of(cls, doc: Document) -> DocRef:
        return cls(doc.id, doc.source, doc.paperless_id, doc.stored_path, doc.mime_type, doc.sha256)


def is_upload(doc) -> bool:
    return doc.source == DocumentSource.UPLOAD


# --- reading ------------------------------------------------------------------------------------------


async def load_pages(doc, *, paperless: Paperless | None = None, use_text: bool = True):
    """(pages, OCR text) for the reading pipeline: something with `len`, `png(index, dpi)` and `close`.

    Paperless: the file is downloaded and drawn in this process (render.Pages), as it always was.
    Upload: nothing of the file is parsed here. The pages are drawn one at a time by a short-lived
    sandbox child (sandbox.SandboxPages): a PDF as it is, a photo made upright, RGB and at most
    render.UPLOAD_IMAGE_SIDE px. An upload has no OCR text: verification then rests on the two readers
    alone."""
    if is_upload(doc):
        return await asyncio.to_thread(_open_upload, doc), ""
    data, media_type, content = await load_file(doc, paperless=paperless, use_text=use_text)
    pages = await asyncio.to_thread(render.Pages, data, media_type)
    return pages, content


async def load_file(doc, *, paperless: Paperless | None = None, use_text: bool = True) -> tuple[bytes, str, str]:
    """(file bytes, media type, OCR text) of a PAPERLESS document. The file stays in memory.

    The original, or Paperless's PDF version when the original is neither a PDF nor an image, and
    (when `use_text`) Paperless's own OCR text as the third source for verification."""
    if is_upload(doc):
        raise SourceError("an uploaded file is not fetched: it is drawn by the sandbox")
    paperless = paperless or Paperless()
    data, media_type = await paperless.download(doc.paperless_id, original=True)
    if data[:5] != b"%PDF-" and not media_type.startswith("image/"):
        # e.g. an office file: Paperless keeps a PDF version of it
        data, media_type = await paperless.download(doc.paperless_id, original=False)
    content = ""
    if use_text:
        content = (await paperless.document(doc.paperless_id)).get("content") or ""
    return data, media_type, content


def _open_upload(doc) -> sandbox.SandboxPages:
    """Runs in a worker thread. The sandbox reads the file by path; here it is only located."""
    path = uploads.stored_file(doc.stored_path)
    if not path:
        raise SourceError("the uploaded file is not available")
    if not os.path.isfile(path):
        raise SourceError("the uploaded file is missing from the data folder")
    kind = uploads.kind_of_mime(doc.mime_type)
    if kind is None:
        raise SourceError("the uploaded file has an unknown type")
    try:
        return sandbox.SandboxPages(path, kind)
    except (sandbox.SandboxError, sandbox.Refused) as exc:
        raise SourceError(f"the uploaded file could not be read: {exc}") from None


# --- showing ------------------------------------------------------------------------------------------

# An upload is medical data served from our own origin: it is never sniffed into another type, never
# cached, and shown inline without a file name (the stored name is ours, the original one is not).
_UPLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Content-Disposition": "inline",
}


# What a Paperless preview / thumbnail may be. The type comes from the other end: anything else (HTML,
# SVG, scripts) would run from this app's own origin, so it is refused.
_PAPERLESS_MEDIA = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
_PAPERLESS_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _paperless_response(content: bytes, media_type: str) -> Response:
    base = (media_type or "").split(";")[0].strip().lower()
    if base not in _PAPERLESS_MEDIA:
        log.warning("Paperless sent a file of an unexpected type; refused")
        raise HTTPException(502, "Paperless could not provide this file right now.")
    return Response(content=content, media_type=base, headers=_PAPERLESS_HEADERS)


async def preview(doc) -> Response:
    if is_upload(doc):
        path = await asyncio.to_thread(uploads.stored_file, doc.stored_path)
        if not path or not await asyncio.to_thread(os.path.isfile, path):
            raise HTTPException(404, "the file is missing from the data folder")
        return FileResponse(path, media_type=doc.mime_type or "application/octet-stream", headers=_UPLOAD_HEADERS)
    content, media_type = await _from_paperless(Paperless().preview(doc.paperless_id))
    return _paperless_response(content, media_type)


async def thumbnail(doc) -> Response:
    if is_upload(doc):
        # The cached file; made again (by the sandbox, never here) only when it is missing.
        data = await uploads.thumbnail_bytes(doc.stored_path, doc.sha256, uploads.kind_of_mime(doc.mime_type) or "")
        if data is None:
            raise HTTPException(404, "no thumbnail: the file is missing or unreadable")
        return Response(content=data, media_type="image/jpeg", headers=_UPLOAD_HEADERS)
    content, media_type = await _from_paperless(Paperless().thumbnail(doc.paperless_id))
    return _paperless_response(content, media_type)


def public_link(doc) -> str | None:
    """Where a person can open the document in Paperless; an upload has no such place."""
    if is_upload(doc) or doc.paperless_id is None:
        return None
    return Paperless.public_link(doc.paperless_id)


def has_file(doc) -> bool:
    """Is there a file to preview? A Paperless document has one there; an upload has it on disk."""
    return uploads.file_exists(doc.stored_path) if is_upload(doc) else doc.paperless_id is not None
