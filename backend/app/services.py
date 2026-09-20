"""Keep the index of medical documents in step with Paperless (uploaded documents are not touched)."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from .db import engine
from .models import Document, DocumentKind, DocumentSource
from .paperless import Paperless, parse_date

log = logging.getLogger("vibehealth")


def _kind_from_tags(tag_names: list[str], kind_map: dict[str, DocumentKind]) -> DocumentKind:
    """The first of the document's tags that the map knows (case ignored), else OTHER."""
    lookup = {name.strip().lower(): kind for name, kind in kind_map.items()}
    for name in tag_names:
        if kind := lookup.get(name.strip().lower()):
            return kind
    return DocumentKind.OTHER


async def sync_documents() -> dict:
    """Record new medical documents and pick up changes to known ones.

    A document that drops out of Paperless's medical set is left in place: the
    user may have hidden or retagged it on purpose, and nothing here is lost by
    keeping the row.
    """
    paperless = Paperless()  # address, token and selection as saved now
    tags = await paperless.tags()
    rows = await paperless.medical_documents(known_tags=tags)

    created = updated = 0
    new_rows: list[Document] = []
    with Session(engine) as session:
        for row in rows:
            title = row.get("title") or f"Document {row['id']}"
            kind = _kind_from_tags([tags.get(t, "") for t in row.get("tags", [])], paperless.kind_map)
            doc_date = parse_date(row.get("created_date") or row.get("created"))

            doc = session.exec(
                select(Document).where(
                    Document.source == DocumentSource.PAPERLESS, Document.paperless_id == row["id"]
                )
            ).first()
            if doc is None:
                doc = Document(
                    source=DocumentSource.PAPERLESS, paperless_id=row["id"], title=title, kind=kind, doc_date=doc_date
                )
                session.add(doc)
                new_rows.append(doc)
                created += 1
            elif (doc.title, doc.kind, doc.doc_date) != (title, kind, doc_date):
                doc.title, doc.kind, doc.doc_date = title, kind, doc_date
                doc.updated_at = datetime.now()
                session.add(doc)
                updated += 1
        session.commit()
        # Candidates for "read new documents after sync": lab reports, and
        # documents whose tags say nothing more specific.
        new_ids = [
            d.id for d in new_rows if d.kind in (DocumentKind.BLOOD_TEST, DocumentKind.OTHER)
        ]

    log.info("sync: %s medical documents, %s new, %s changed", len(rows), created, updated)
    return {"seen": len(rows), "created": created, "updated": updated, "new_ids": new_ids}
