"""Database tables.

VibeHealth keeps an index of the medical documents and the lab values read from
them. A document either comes from Paperless (the file stays in Paperless) or was
uploaded (the original is kept on disk under `data/uploads`, see uploads.py).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Index, String, text
from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now()


class DocumentKind(StrEnum):
    """Taken from the document's Paperless tags."""

    BLOOD_TEST = "blood_test"
    REPORT = "report"
    PRESCRIPTION = "prescription"
    IMAGING = "imaging"
    OTHER = "other"


class DocumentSource(StrEnum):
    PAPERLESS = "paperless"
    UPLOAD = "upload"


class Document(SQLModel, table=True):
    __tablename__ = "documents"
    # An uploaded file is stored once: the same content cannot be added twice.
    __table_args__ = (
        Index("ux_documents_sha256_upload", "sha256", unique=True, sqlite_where=text("source = 'upload'")),
    )

    id: int | None = Field(default=None, primary_key=True)
    # NULL for an upload (SQLite allows any number of NULLs in a unique index).
    paperless_id: int | None = Field(default=None, index=True, unique=True)
    title: str = ""
    kind: DocumentKind = DocumentKind.OTHER
    doc_date: date | None = None
    # Hidden by the user: Paperless tags are not always right, and a receipt
    # that picked up a medical tag should not stay in the list.
    ignored: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    # Where the document comes from. The database default keeps the previous release working on a
    # migrated database: it never names this column and its rows are all Paperless rows.
    source: str = Field(default="paperless", index=True, sa_column_kwargs={"server_default": "paperless"})
    # Uploads only. `original_filename` is sanitised and for display; `stored_path` is relative to
    # data/uploads and never comes from the client.
    original_filename: str | None = None
    stored_path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


# --- reading lab values --------------------------------------------------------
# New tables rather than new columns: init_db only creates missing tables.


class AppSetting(SQLModel, table=True):
    """Settings changed in the app. The environment supplies the defaults."""

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str = ""  # JSON
    updated_at: datetime = Field(default_factory=now)


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    CLEARED = "cleared"  # its values were cleared: the document counts as not read


class ExtractionRun(SQLModel, table=True):
    """One reading of one document."""

    __tablename__ = "extraction_runs"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True, ondelete="CASCADE")
    status: RunStatus = Field(default=RunStatus.RUNNING, sa_type=String)
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    duration_s: float | None = None
    pages: int = 0
    page_errors: str = "[]"  # JSON list of {page, reader, error}
    error: str = ""
    settings: str = "{}"  # JSON: the reading settings used
    verified: int = 0
    needs_review: int = 0
    kept_approved: int = 0


class ValueStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class ExtractedValue(SQLModel, table=True):
    """A lab value read from a document. Only `approved` rows count as data."""

    __tablename__ = "extracted_values"
    # At most one approved value per test per document.
    __table_args__ = (
        Index(
            "ux_extracted_values_approved",
            "document_id",
            "test_code",
            unique=True,
            sqlite_where=text("status = 'approved'"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="documents.id", index=True, ondelete="CASCADE")
    run_id: int | None = Field(default=None, foreign_key="extraction_runs.id", ondelete="SET NULL")
    test_code: str | None = Field(default=None, index=True)
    raw_name: str = ""
    value_text: str = ""
    value_num: float | None = None
    unit: str = ""
    ref_range: str = ""
    flag: str = ""  # "H", "L" or ""
    # Stored as the plain value ('approved'), which the partial index above relies on.
    status: ValueStatus = Field(default=ValueStatus.NEEDS_REVIEW, index=True, sa_type=String)
    reason: str = ""
    page: int | None = None
    reader_a: str | None = None
    reader_b: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
