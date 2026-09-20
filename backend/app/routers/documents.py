"""The medical documents known to VibeHealth, and the values read from them.

A document is either a Paperless one (the file stays in Paperless) or an upload (the original is
kept in the data folder: see uploads.py and sources.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from .. import reading_settings, report, settings_store, sources, uploads
from ..catalog import BY_CODE, ORDER, flag_for, num, to_canonical_unit
from ..db import engine, get_session
from ..middleware import set_body_limit
from ..models import (
    Document,
    DocumentKind,
    DocumentReport,
    DocumentSource,
    DocumentText,
    ExtractedValue,
    ExtractionRun,
    ValueStatus,
    reads_lab_values,
)
from ..reading import approved_codes, clear_unapproved, mark_cleared, reading_states, run_summary
from ..security import password_hash, require_session
from ..worker import begin_delete, end_delete, is_busy, request_read, state

log = logging.getLogger("vibehealth")

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(require_session)])
values_router = APIRouter(prefix="/api/values", tags=["values"], dependencies=[Depends(require_session)])
dashboard_router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(require_session)])
examinations_router = APIRouter(
    prefix="/api/examinations", tags=["examinations"], dependencies=[Depends(require_session)]
)


class DocumentOut(BaseModel):
    id: int
    source: str  # "paperless" | "upload"
    paperless_id: int | None  # null for an upload
    title: str
    kind: str
    doc_date: date | None
    ignored: bool
    paperless_link: str | None  # null for an upload
    original_filename: str | None  # uploads: the cleaned name to show; else null
    mime_type: str | None  # uploads: application/pdf, image/jpeg, image/png, image/webp
    size_bytes: int | None  # uploads
    has_file: bool  # a preview / thumbnail can be shown (an upload whose file went missing: false)
    # "lab": lab values are read from it; "text": it is a narrative report (findings and a conclusion). See models.LAB_KINDS.
    read_mode: str = "lab"
    reading: dict | None = None
    # A text report that has been read: {status, findings} of its automatic summary; else null.
    report: dict | None = None


def _states(session: Session, ids: list[int]) -> dict[int, dict]:
    reading = state["reading"]
    return reading_states(session, ids, reading["queue"], reading["current"])


def _out(doc: Document, reading: dict | None = None, summary: dict | None = None) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        source=doc.source,
        paperless_id=doc.paperless_id,
        title=doc.title,
        kind=doc.kind,
        doc_date=doc.doc_date,
        ignored=doc.ignored,
        paperless_link=sources.public_link(doc),
        original_filename=doc.original_filename,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        has_file=sources.has_file(doc),
        read_mode="lab" if reads_lab_values(doc.kind) else "text",
        reading=reading,
        report=summary,
    )


def _outs(session: Session, docs: list[Document]) -> list[DocumentOut]:
    ids = [d.id for d in docs]
    states, summaries = _states(session, ids), report.summaries(session, ids)
    return [_out(d, states.get(d.id), summaries.get(d.id)) for d in docs]


def _get(session: Session, document_id: int) -> Document:
    doc = session.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "document not found")
    return doc


@router.get("")
def list_documents(
    include_ignored: bool = False, session: Session = Depends(get_session)
) -> list[DocumentOut]:
    query = select(Document).order_by(Document.doc_date.desc(), Document.id.desc())
    if not include_ignored:
        query = query.where(Document.ignored == False)  # noqa: E712 - SQL comparison
    return _outs(session, session.exec(query).all())


@router.get("/{document_id}")
def get_document(document_id: int, session: Session = Depends(get_session)) -> DocumentOut:
    return _outs(session, [_get(session, document_id)])[0]


@router.get("/{document_id}/thumbnail")
async def thumbnail(document_id: int, session: Session = Depends(get_session)) -> Response:
    doc = _get(session, document_id)
    session.expunge(doc)  # the session is not used again while the file is read
    return await sources.thumbnail(doc)


@router.get("/{document_id}/preview")
async def preview(document_id: int, session: Session = Depends(get_session)) -> Response:
    doc = _get(session, document_id)
    session.expunge(doc)
    return await sources.preview(doc)


class IgnoreBody(BaseModel):
    ignored: bool = True


@router.post("/{document_id}/ignore")
def ignore(
    document_id: int, body: IgnoreBody | None = None, session: Session = Depends(get_session)
) -> DocumentOut:
    """Hide a document that is not really medical, or bring it back."""
    doc = _get(session, document_id)
    doc.ignored = body.ignored if body else True
    doc.updated_at = datetime.now()
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return _outs(session, [doc])[0]


# --- uploads -------------------------------------------------------------------

UPLOAD_PATH = "/api/documents/upload"
# The body limit for this one route follows the setting: the file's cap plus the multipart wrapping.
set_body_limit(
    UPLOAD_PATH,
    lambda: uploads.cap_bytes(settings_store.value("uploads", "max_file_mb")) + uploads.MULTIPART_OVERHEAD,
)

_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "kind": {"type": "string", "enum": [k.value for k in DocumentKind], "default": "other"},
                "title": {"type": "string", "maxLength": uploads.MAX_TITLE},
                "doc_date": {"type": "string", "format": "date"},
                "read_now": {"type": "boolean", "default": False},
            },
        }}},
    }
}


class _Duplicate(Exception):
    def __init__(self, document_id: int) -> None:
        self.document_id = document_id


@dataclass
class _Prepared:
    file_type: str
    kind: DocumentKind
    doc_date: date | None
    title: str | None
    read_now: bool


def _prepare_upload(received: uploads.Received) -> _Prepared:
    """The step after the bytes have arrived and before the file is opened (runs in a worker thread):
    the form fields, the type from the magic bytes, the duplicate check. Nothing here parses the file."""
    fields = received.fields
    kind_field = uploads.parse_kind(fields["kind"]) if fields.get("kind", "").strip() else DocumentKind.OTHER
    date_field = uploads.parse_date(fields.get("doc_date", ""))
    read_now = uploads.parse_bool(fields.get("read_now", ""))
    title = None
    if fields.get("title", "").strip():
        try:
            title = uploads.clean_title(fields["title"])
        except ValueError:
            raise uploads.UploadError(422, f"title must be 1 to {uploads.MAX_TITLE} characters") from None

    file_type = uploads.sniff(received.head)
    if file_type is None:
        raise uploads.UploadError(415, f"only {uploads.ACCEPTED} files are accepted")
    if (dup := _existing_upload(received.sha256)) is not None:
        raise _Duplicate(dup)
    return _Prepared(file_type, kind_field, date_field, title, read_now)


def _existing_upload(sha256: str) -> int | None:
    with Session(engine) as session:
        return session.exec(
            select(Document.id).where(Document.source == DocumentSource.UPLOAD, Document.sha256 == sha256)
        ).first()


def _store_upload(received: uploads.Received, prepared: _Prepared, checked: uploads.Checked) -> int:
    """The file has been checked by the sandbox: keep it and add the row (runs in a worker thread).
    Returns the document id. The temporary file is the caller's to remove."""
    relative = uploads.store(received.path, prepared.file_type)  # from here the file is in place: undo on failure
    try:
        stem = uploads.name_stem(received.filename)
        with Session(engine) as session:
            doc = Document(
                source=DocumentSource.UPLOAD,
                title=prepared.title or stem,
                kind=prepared.kind,
                doc_date=prepared.doc_date,
                original_filename=uploads.display_name(received.filename, prepared.file_type),
                stored_path=relative,
                mime_type=uploads.TYPES[prepared.file_type][0],
                size_bytes=received.size,
                sha256=received.sha256,
            )
            session.add(doc)
            try:
                session.commit()
            except IntegrityError:  # the same file arrived twice at once: the unique index decided
                session.rollback()
                dup = _existing_upload(received.sha256)
                if dup is None:
                    raise
                raise _Duplicate(dup) from None
            session.refresh(doc)
            doc_id = doc.id
    except BaseException as exc:
        # (not the thumbnail of a duplicate: it belongs to the first upload of the same bytes)
        uploads.delete_files(relative, None if isinstance(exc, _Duplicate) else received.sha256)
        raise
    uploads.write_thumbnail(received.sha256, checked.thumbnail)  # the sandbox drew it with the check
    return doc_id


def _upload_error(exc: uploads.UploadError) -> HTTPException:
    return HTTPException(exc.status, exc.message, headers=exc.headers)


@router.post("/upload", status_code=201, openapi_extra=_UPLOAD_BODY)
async def upload_document(request: Request) -> Response:
    """Add a document from a file: multipart/form-data with `file` and, optionally, `kind`, `title`,
    `doc_date` (YYYY-MM-DD) and `read_now`. Only PDF, JPEG, PNG and WebP, and only once a password
    is set. 201 {document, read_queued}; 409 {detail, document_id} when the same file is already there.
    Also: 429 (with Retry-After) while other uploads are being processed, 507 when the disk or the
    uploads folder has no room, 408 for a body that stalls or crawls, 422 for a file that cannot be read."""
    cfg = settings_store.resolve("uploads", use_cache=False).values
    if not cfg["enabled"]:
        raise HTTPException(403, "Uploads are turned off in Settings.")
    if not password_hash():
        raise HTTPException(
            403, "Set a password first: documents can only be uploaded once the app is protected by a password."
        )
    received: uploads.Received | None = None
    try:
        try:
            others = uploads.begin_upload()
        except uploads.UploadError as exc:
            raise _upload_error(exc) from None
        try:
            cap = uploads.cap_bytes(cfg["max_file_mb"])
            max_total = int(cfg["max_total_mb"]) * 1024 * 1024
            declared = _declared_length(request)
            used = await asyncio.to_thread(uploads.check_room, cap, max_total, others, declared)
            received = await uploads.receive(request, cap, max(0, max_total - used))
            prepared = await asyncio.to_thread(_prepare_upload, received)
            checked = await uploads.check(received.path, prepared.file_type)  # in the sandbox
            doc_id = await asyncio.to_thread(_store_upload, received, prepared, checked)
        finally:
            uploads.end_upload()
    except uploads.UploadError as exc:
        raise _upload_error(exc) from None
    except _Duplicate as dup:
        return JSONResponse(
            {"detail": "This file has already been uploaded.", "document_id": dup.document_id}, status_code=409
        )
    finally:
        if received is not None:
            uploads.remove_quietly(received.path)  # gone already when it was stored

    queued = False
    if prepared.read_now and reading_settings.load().enabled:
        queued = bool(request_read([doc_id]))
    with Session(engine) as session:
        doc = _get(session, doc_id)
        body = {"document": _outs(session, [doc])[0].model_dump(mode="json"), "read_queued": queued}
    return JSONResponse(body, status_code=201)


def _declared_length(request: Request) -> int | None:
    try:
        n = int(request.headers.get("content-length", ""))
    except ValueError:
        return None
    return n if n > 0 else None


class DocumentPatch(BaseModel):
    """What can be changed on an uploaded document. Send only what changes; `doc_date: null` clears the date."""

    title: str | None = Field(default=None, max_length=1000)
    kind: DocumentKind | None = None
    doc_date: date | None = None

    model_config = {"extra": "forbid"}

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        return None if v is None else uploads.clean_title(v)

    @field_validator("doc_date")
    @classmethod
    def _doc_date(cls, v: date | None) -> date | None:
        return None if v is None else uploads.check_doc_date(v)


def _only_uploads(doc: Document, what: str) -> None:
    if doc.source != DocumentSource.UPLOAD:
        raise HTTPException(409, f"This document comes from Paperless: {what}")


@router.patch("/{document_id}")
def edit_document(document_id: int, body: DocumentPatch, session: Session = Depends(get_session)) -> DocumentOut:
    """Change the title, kind or date of an UPLOADED document (Paperless documents are edited in
    Paperless: a sync would undo it here)."""
    doc = _get(session, document_id)
    _only_uploads(doc, "change its title, kind and date there.")
    sent = body.model_fields_set
    if ("title" in sent and body.title is None) or ("kind" in sent and body.kind is None):
        raise HTTPException(422, "title and kind cannot be null")
    if "title" in sent:
        doc.title = body.title
    if "kind" in sent:
        doc.kind = body.kind
    if "doc_date" in sent:
        doc.doc_date = body.doc_date
    if sent:
        doc.updated_at = datetime.now()
        session.add(doc)
        session.commit()
        session.refresh(doc)
    return _outs(session, [doc])[0]


@router.delete("/{document_id}")
def delete_document(document_id: int, session: Session = Depends(get_session)) -> dict:
    """Delete an UPLOADED document: its values and readings, the row, and then the original file, the
    thumbnail and the cache. 409 for a Paperless document (hide it instead) and for one being read."""
    doc = _get(session, document_id)
    _only_uploads(doc, "it cannot be deleted here. Hide it instead (POST /api/documents/{id}/ignore).")
    if not begin_delete(document_id):
        raise HTTPException(409, "This document is being read: delete it when the reading has finished.")
    stored_path, sha = doc.stored_path, doc.sha256
    # The intent to remove the file is written BEFORE the row goes: if the process dies between the
    # commit and the unlink, the next start finishes the job (if the commit fails instead, the entry
    # is harmless: the start-up sweep leaves a path that a document owns).
    noted = uploads.note_pending(stored_path)
    try:
        session.execute(delete(ExtractedValue).where(col(ExtractedValue.document_id) == document_id))
        session.execute(delete(ExtractionRun).where(col(ExtractionRun.document_id) == document_id))
        session.execute(delete(DocumentText).where(col(DocumentText.document_id) == document_id))
        session.execute(delete(DocumentReport).where(col(DocumentReport.document_id) == document_id))
        session.delete(doc)
        session.commit()
    finally:
        end_delete(document_id)
    # The database is final. A file that cannot be removed now is logged and retried at the next start:
    # it must not turn a delete that happened into an error.
    return {"deleted": True, "file_removed": uploads.delete_files(stored_path, sha, noted=noted)}


# --- reading -------------------------------------------------------------------


class ValueOut(BaseModel):
    id: int
    test_code: str | None
    name_en: str
    name_el: str
    raw_name: str
    value: str
    value_num: float | None
    unit: str
    ref_range: str
    flag: str
    status: str
    reason: str
    page: int | None
    reader_a: str | None
    reader_b: str | None


def _value_out(v: ExtractedValue) -> ValueOut:
    test = BY_CODE.get(v.test_code or "")
    return ValueOut(
        id=v.id, test_code=v.test_code,
        name_en=test.name_en if test else "", name_el=test.name_el if test else "",
        raw_name=v.raw_name, value=v.value_text, value_num=v.value_num, unit=v.unit,
        ref_range=v.ref_range, flag=v.flag, status=v.status, reason=v.reason, page=v.page,
        reader_a=v.reader_a, reader_b=v.reader_b,
    )


@router.post("/{document_id}/read")
def read(document_id: int, as_lab: bool = False, session: Session = Depends(get_session)) -> dict:
    """Queue a reading. A text report (imaging, opinion, prescription) is transcribed and summarised;
    `as_lab=true` reads it for lab values instead (a report with a page of blood results in it)."""
    _get(session, document_id)
    if not reading_settings.load().enabled:
        raise HTTPException(409, "reading is turned off in Settings")
    added = request_read([document_id], force_lab=as_lab)
    return {"queued": bool(added), "already": not added}


@router.get("/{document_id}/report")
def get_report(document_id: int, session: Session = Depends(get_session)) -> dict:
    """What was read from a text report: the automatic summary (conclusion, key findings, how it went), the
    text of every page and the pages that look like lab results. `report` is null until it has been read."""
    doc = _get(session, document_id)
    return {"document": _outs(session, [doc])[0], "report": report.detail(session, document_id)}


@router.get("/{document_id}/values")
def values(document_id: int, session: Session = Depends(get_session)) -> dict:
    doc = _get(session, document_id)
    rows = session.exec(select(ExtractedValue).where(ExtractedValue.document_id == document_id)).all()
    rows = sorted(rows, key=lambda v: (v.page or 0, ORDER.get(v.test_code or "", 999), v.id))
    run = session.exec(
        select(ExtractionRun)
        .where(ExtractionRun.document_id == document_id)
        .order_by(col(ExtractionRun.id).desc())
    ).first()
    return {
        "document": _outs(session, [doc])[0],
        "last_run": run_summary(run) if run else None,
        "values": [_value_out(v) for v in rows],
    }


@router.delete("/{document_id}/values")
def clear_values(document_id: int, session: Session = Depends(get_session)) -> dict:
    """Delete every value that is not approved."""
    _get(session, document_id)
    if is_busy(document_id):
        raise HTTPException(409, "this document is being read")
    deleted = clear_unapproved(session, document_id)
    mark_cleared(session, document_id)
    session.commit()
    return {"deleted": deleted}


@router.post("/{document_id}/values/approve-verified")
def approve_verified(document_id: int, session: Session = Depends(get_session)) -> dict:
    _get(session, document_id)
    taken = approved_codes(session, document_id)
    approved = skipped = 0
    rows = session.exec(
        select(ExtractedValue).where(
            ExtractedValue.document_id == document_id,
            ExtractedValue.status == ValueStatus.VERIFIED,
        ).order_by(ExtractedValue.id)
    ).all()
    for v in rows:
        if not v.test_code or v.test_code in taken:
            skipped += 1
            continue
        v.status = ValueStatus.APPROVED
        v.updated_at = datetime.now()
        taken.add(v.test_code)
        session.add(v)
        approved += 1
    session.commit()
    return {"approved": approved, "skipped": skipped}


class ApproveBody(BaseModel):
    """Optional corrections, applied before approving."""

    test_code: str | None = None
    value: str | None = Field(default=None, max_length=100)
    unit: str | None = Field(default=None, max_length=40)
    ref_range: str | None = Field(default=None, max_length=80)


def _get_value(session: Session, value_id: int) -> ExtractedValue:
    v = session.get(ExtractedValue, value_id)
    if not v:
        raise HTTPException(404, "value not found")
    return v


@values_router.post("/{value_id}/approve")
def approve(value_id: int, body: ApproveBody | None = None, session: Session = Depends(get_session)) -> ValueOut:
    v = _get_value(session, value_id)
    body = body or ApproveBody()
    if body.test_code is not None:
        if body.test_code not in BY_CODE:
            raise HTTPException(422, "unknown test")
        v.test_code = body.test_code
    if body.value is not None:
        if not body.value.strip():
            raise HTTPException(422, "the value is empty")
        v.value_text = body.value.strip()
    if body.unit is not None:
        v.unit = body.unit.strip()
    if body.ref_range is not None:
        v.ref_range = body.ref_range.strip()
    if not v.test_code:
        raise HTTPException(422, "choose the test before approving")
    edited = any(x is not None for x in (body.test_code, body.value, body.unit, body.ref_range))
    if edited:
        v.value_num, v.unit = to_canonical_unit(v.test_code, num(v.value_text), v.unit)
        v.flag = flag_for(v.value_text, v.ref_range)
        v.reason = "edited at review"
    other = session.exec(
        select(ExtractedValue).where(
            ExtractedValue.document_id == v.document_id,
            ExtractedValue.test_code == v.test_code,
            ExtractedValue.status == ValueStatus.APPROVED,
            ExtractedValue.id != v.id,
        )
    ).first()
    if other:
        raise HTTPException(409, "this test already has an approved value in this document")
    v.status = ValueStatus.APPROVED
    v.updated_at = datetime.now()
    session.add(v)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "this test already has an approved value in this document") from exc
    session.refresh(v)
    return _value_out(v)


@values_router.post("/{value_id}/reject")
def reject(value_id: int, session: Session = Depends(get_session)) -> ValueOut:
    """Also undoes an approval: the row stays until the next reading or Clear."""
    v = _get_value(session, value_id)
    v.status = ValueStatus.REJECTED
    v.updated_at = datetime.now()
    session.add(v)
    session.commit()
    session.refresh(v)
    return _value_out(v)


# --- examinations / dashboard: approved values across documents ---------------


class FlaggedValueOut(BaseModel):
    """An approved value outside its reference range, for the dashboard."""

    id: int
    document_id: int
    document_title: str
    doc_date: date | None
    test_code: str | None
    name_en: str
    name_el: str
    value: str
    unit: str
    ref_range: str
    flag: str


def _flagged_query():
    return (
        select(ExtractedValue, Document)
        .join(Document, col(ExtractedValue.document_id) == col(Document.id))
        .where(
            ExtractedValue.status == ValueStatus.APPROVED,
            col(ExtractedValue.flag).in_(["H", "L"]),
            Document.ignored == False,  # noqa: E712
        )
        .order_by(col(Document.doc_date).desc(), col(ExtractedValue.id).desc())
    )


def _flagged_out(v: ExtractedValue, doc: Document) -> FlaggedValueOut:
    test = BY_CODE.get(v.test_code or "")
    return FlaggedValueOut(
        id=v.id, document_id=doc.id, document_title=doc.title, doc_date=doc.doc_date,
        test_code=v.test_code, name_en=test.name_en if test else "", name_el=test.name_el if test else "",
        value=v.value_text, unit=v.unit, ref_range=v.ref_range, flag=v.flag,
    )


@values_router.get("/flagged")
def flagged_values(
    limit: int = Query(50, ge=1, le=200), session: Session = Depends(get_session)
) -> list[FlaggedValueOut]:
    """Approved values flagged H or L, most recent document first."""
    rows = session.exec(_flagged_query().limit(limit)).all()
    return [_flagged_out(v, doc) for v, doc in rows]


class TestHistoryPoint(BaseModel):
    document_id: int
    document_title: str
    doc_date: date | None
    value: str
    value_num: float | None
    unit: str
    ref_range: str
    flag: str


class TestSummaryOut(BaseModel):
    test_code: str
    name_en: str
    name_el: str
    category: str
    latest: TestHistoryPoint
    history: list[TestHistoryPoint]
    # Set when this row merges a percentage test with its paired absolute-count
    # test (e.g. NEUT % + NEUT_ABS). `latest`/`history` above stay the
    # percentage's own points; `secondary*` carries the absolute count's.
    secondary: TestHistoryPoint | None = None
    secondary_history: list[TestHistoryPoint] | None = None


def _merge_plan(available: set[str]) -> list[tuple[str, str | None]]:
    """(primary_code, secondary_code | None) pairs for a set of test codes present
    in the data: merges each percentage test with its paired absolute-count test
    (see `Test.paired_with`) when both are available, leaving everything else as
    its own row. The absolute code never appears twice: once matched as a
    secondary it is dropped from the plan.

    Shared by `/api/values/by-test` and the `blood_test` `examinations()` view.
    """
    consumed: set[str] = {
        test.paired_with
        for code in available
        if (test := BY_CODE.get(code)) and test.paired_with and test.paired_with in available
    }
    plan: list[tuple[str, str | None]] = []
    for code in sorted(available):
        if code in consumed:
            continue
        test = BY_CODE.get(code)
        pair_code = test.paired_with if test else None
        if pair_code and pair_code in available:
            plan.append((code, pair_code))
        else:
            plan.append((code, None))
    return plan


def _merge_paired_summaries(grouped: dict[str, list[TestHistoryPoint]]) -> list[TestSummaryOut]:
    """Group per-test history points into `TestSummaryOut` rows, combining each
    percentage/absolute-count pair (NEUT/NEUT_ABS, etc.) into one display row.

    Extraction, verification and storage are untouched: this only reshapes
    already-approved values for display.
    """
    def summary(code: str, points: list[TestHistoryPoint]) -> TestSummaryOut:
        points = sorted(points, key=lambda p: p.doc_date or date.min, reverse=True)
        test = BY_CODE.get(code)
        return TestSummaryOut(
            test_code=code, name_en=test.name_en if test else code, name_el=test.name_el if test else code,
            category=test.category if test else "other", latest=points[0], history=points,
        )

    out: list[TestSummaryOut] = []
    for code, pair_code in _merge_plan(set(grouped)):
        row = summary(code, grouped[code])
        if pair_code:
            pair_row = summary(pair_code, grouped[pair_code])
            row.secondary, row.secondary_history = pair_row.latest, pair_row.history
        out.append(row)
    return out


@values_router.get("/by-test")
def values_by_test(session: Session = Depends(get_session)) -> list[TestSummaryOut]:
    """Approved lab values grouped by test, newest first within each test.

    The five CBC differential pairs (NEUT/NEUT_ABS, ...) are merged into one
    row each for display: see `_merge_paired_summaries`.

    TODO: once there is enough history, `history` is the natural input for a
    per-test trend chart. Today only `latest` is shown in the Examinations page.
    """
    rows = session.exec(
        select(ExtractedValue, Document)
        .join(Document, col(ExtractedValue.document_id) == col(Document.id))
        .where(
            ExtractedValue.status == ValueStatus.APPROVED,
            col(ExtractedValue.test_code).is_not(None),
            Document.ignored == False,  # noqa: E712
        )
    ).all()
    grouped: dict[str, list[TestHistoryPoint]] = {}
    for v, doc in rows:
        grouped.setdefault(v.test_code, []).append(TestHistoryPoint(
            document_id=doc.id, document_title=doc.title, doc_date=doc.doc_date,
            value=v.value_text, value_num=v.value_num, unit=v.unit, ref_range=v.ref_range, flag=v.flag,
        ))
    out = _merge_paired_summaries(grouped)
    out.sort(key=lambda t: ORDER.get(t.test_code, 999))
    return out


# --- dashboard -------------------------------------------------------------


class CategoryCountOut(BaseModel):
    kind: str
    count: int
    last_date: date | None


class DashboardSummaryOut(BaseModel):
    recent_documents: list[DocumentOut]
    flagged_values: list[FlaggedValueOut]
    category_counts: list[CategoryCountOut]
    has_any_approved: bool


@dashboard_router.get("/summary")
def dashboard_summary(
    recent_limit: int = Query(5, ge=1, le=50),
    flagged_limit: int = Query(5, ge=1, le=50),
    session: Session = Depends(get_session),
) -> DashboardSummaryOut:
    """Everything the home page needs in one call."""
    recent_docs = session.exec(
        select(Document)
        .where(Document.ignored == False)  # noqa: E712
        .order_by(Document.doc_date.desc(), Document.id.desc())
        .limit(recent_limit)
    ).all()
    recent_documents = _outs(session, recent_docs)

    flagged_rows = session.exec(_flagged_query().limit(flagged_limit)).all()
    flagged_values_out = [_flagged_out(v, doc) for v, doc in flagged_rows]

    category_rows = session.exec(
        select(Document.kind, func.count(), func.max(Document.doc_date))
        .where(Document.ignored == False)  # noqa: E712
        .group_by(Document.kind)
    ).all()
    category_counts = [
        CategoryCountOut(kind=kind, count=count, last_date=last_date)
        for kind, count, last_date in category_rows
    ]

    has_any_approved = session.exec(
        select(ExtractedValue.id).where(ExtractedValue.status == ValueStatus.APPROVED).limit(1)
    ).first() is not None

    return DashboardSummaryOut(
        recent_documents=recent_documents,
        flagged_values=flagged_values_out,
        category_counts=category_counts,
        has_any_approved=has_any_approved,
    )


# --- examinations ------------------------------------------------------------


class ExamTestRowOut(BaseModel):
    test_code: str
    name_en: str
    name_el: str
    value: str
    value_num: float | None
    unit: str
    ref_range: str
    flag: str
    doc_date: date | None
    document_id: int
    # Set when this row merges a percentage test with its paired absolute-count
    # test's latest value (e.g. NEUT % + NEUT_ABS): see `_merge_plan`.
    secondary: TestHistoryPoint | None = None


class ExamCategoryOut(BaseModel):
    category: str
    tests: list[ExamTestRowOut]


class ExaminationsOut(BaseModel):
    kind: str
    categories: list[ExamCategoryOut] | None = None
    documents: list[DocumentOut] | None = None


@examinations_router.get("")
def examinations(
    kind: DocumentKind = DocumentKind.BLOOD_TEST, session: Session = Depends(get_session)
) -> ExaminationsOut:
    """Data for the Examinations page: blood tests grouped by category, or a
    plain document list for every other kind."""
    if kind != DocumentKind.BLOOD_TEST:
        docs = session.exec(
            select(Document)
            .where(Document.kind == kind, Document.ignored == False)  # noqa: E712
            .order_by(Document.doc_date.desc(), Document.id.desc())
        ).all()
        return ExaminationsOut(kind=kind, documents=_outs(session, docs))

    rows = session.exec(
        select(ExtractedValue, Document)
        .join(Document, col(ExtractedValue.document_id) == col(Document.id))
        .where(
            ExtractedValue.status == ValueStatus.APPROVED,
            col(ExtractedValue.test_code).is_not(None),
            Document.ignored == False,  # noqa: E712
        )
    ).all()
    latest: dict[str, tuple[ExtractedValue, Document]] = {}
    for v, doc in rows:
        current = latest.get(v.test_code)
        key = (doc.doc_date or date.min, v.id)
        if current is None or key > (current[1].doc_date or date.min, current[0].id):
            latest[v.test_code] = (v, doc)

    by_category: dict[str, list[ExamTestRowOut]] = {}
    for code, pair_code in _merge_plan(set(latest)):
        v, doc = latest[code]
        test = BY_CODE.get(code)
        category = test.category if test else "other"
        secondary = None
        if pair_code:
            pv, pdoc = latest[pair_code]
            secondary = TestHistoryPoint(
                document_id=pdoc.id, document_title=pdoc.title, doc_date=pdoc.doc_date,
                value=pv.value_text, value_num=pv.value_num, unit=pv.unit, ref_range=pv.ref_range, flag=pv.flag,
            )
        by_category.setdefault(category, []).append(ExamTestRowOut(
            test_code=code, name_en=test.name_en if test else code, name_el=test.name_el if test else code,
            value=v.value_text, value_num=v.value_num, unit=v.unit, ref_range=v.ref_range, flag=v.flag,
            doc_date=doc.doc_date, document_id=doc.id, secondary=secondary,
        ))

    categories = []
    for category, tests in by_category.items():
        tests.sort(key=lambda t: ORDER.get(t.test_code, 999))
        categories.append(ExamCategoryOut(category=category, tests=tests))
    categories.sort(key=lambda c: min((ORDER.get(t.test_code, 999) for t in c.tests), default=999))
    return ExaminationsOut(kind=kind, categories=categories)
