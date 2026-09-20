"""Search over what was read from text reports: page text, summary, key findings, measurements, diagnoses, medications.

One row per document in `document_search` holds all of it folded (lower case, no accents, final sigma made sigma: see
textfold.py), so "Ήπαρ", "ηπαρ" and "ΗΠΑΡ" find each other, and a search is a plain `LIKE '%term%'` on that column. This is
deliberate instead of FTS5: it needs nothing from the SQLite build the image happens to have, it matches inside words
(Greek endings vary: "πολύποδας", "πολύποδα"), and a personal collection is small. Every term must be found (in the
body or in the document's title); the snippets are cut from the ORIGINAL text.

The index is rebuilt by every reading (reading.save_report). A report read before migration 4 has no row yet: a search
indexes such documents first (`ensure_indexed`), so nothing has to be migrated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete
from sqlmodel import Session, col, select

from .models import Document, DocumentReport, DocumentSearch, DocumentText
from .report import _findings, _strings, details_of
from .textfold import fold, nfc

MAX_RESULTS = 30
MAX_SNIPPETS = 3
MAX_TERMS = 6
MIN_TERM = 2
RADIUS = 70  # characters of context on each side of a match


@dataclass(frozen=True)
class Field:
    field: str  # "title", "summary", "finding", "region", "measurement", "diagnosis", ..., "text"
    text: str
    page: int | None = None


def fields(report: DocumentReport | None, pages: list[DocumentText]) -> list[Field]:
    """Everything of a document that search looks through, most telling first (the page text comes last)."""
    out: list[Field] = []
    if report is not None:
        out.append(Field("summary", report.conclusion))
        out += [Field("finding", f) for f in _findings(report)]
        d = details_of(report)
        out += [Field("region", r) for r in _strings(d.get("regions"))]
        for m in d.get("measurements", []) if isinstance(d.get("measurements"), list) else []:
            if isinstance(m, dict):
                out.append(Field("measurement", " ".join(str(m.get(k) or "") for k in ("label", "value", "unit"))))
        for key, name in (("diagnoses", "diagnosis"), ("recommendations", "recommendation")):
            out += [Field(name, x) for x in _strings(d.get(key))]
        follow = d.get("follow_up") if isinstance(d.get("follow_up"), dict) else {}
        for name, label in (("doctor", d.get("doctor")), ("doctor", d.get("specialty")), ("doctor", d.get("prescriber")),
                            ("follow_up", follow.get("text"))):
            if isinstance(label, str):
                out.append(Field(name, label))
        for med in d.get("medications", []) if isinstance(d.get("medications"), list) else []:
            if isinstance(med, dict):
                out.append(Field("medication", " ".join(
                    str(med.get(k) or "") for k in ("name", "active_substance", "strength", "dose_instruction",
                                                   "duration_or_quantity"))))
    out += [Field("text", p.text, p.page) for p in pages]
    return [f for f in out if f.text.strip()]


def index_document(session: Session, document_id: int) -> None:
    """(Re)build the search row of a document from its stored report and page text. The caller commits."""
    report = session.get(DocumentReport, document_id)
    if report is None:
        forget(session, document_id)
        return
    pages = session.exec(select(DocumentText).where(DocumentText.document_id == document_id)).all()
    body = "\n".join(fold(nfc(f.text)) for f in fields(report, list(pages)))
    row = session.get(DocumentSearch, document_id) or DocumentSearch(document_id=document_id)
    row.body = body
    session.add(row)


def forget(session: Session, document_id: int) -> None:
    session.execute(delete(DocumentSearch).where(col(DocumentSearch.document_id) == document_id))


def ensure_indexed(session: Session) -> int:
    """Index the documents that have a report but no search row yet (read before the index existed)."""
    missing = session.exec(
        select(DocumentReport.document_id).where(
            col(DocumentReport.document_id).not_in(select(DocumentSearch.document_id))
        )
    ).all()
    for document_id in missing:
        index_document(session, document_id)
    if missing:
        session.commit()
    return len(missing)


def terms_of(query: str) -> list[str]:
    """The folded words of a query (at most MAX_TERMS, each at least MIN_TERM characters, no repeats)."""
    out: list[str] = []
    for word in fold(nfc(query)).split():
        word = word.strip(".,;:!?()[]{}\"'")
        if len(word) >= MIN_TERM and word not in out:
            out.append(word)
    return out[:MAX_TERMS]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(item: Field, terms: list[str]) -> dict | None:
    """The first match of any term in `item`, with its surroundings, cut from the original text. Because folding keeps
    the length, a position in the folded text is the same position in the original."""
    text = nfc(item.text)
    folded = fold(text)
    hits = [(folded.find(t), t) for t in terms if t in folded]
    if not hits:
        return None
    start, term = min(hits)
    end = start + len(term)
    lo, hi = max(0, start - RADIUS), min(len(text), end + RADIUS)

    def flat(part: str) -> str:
        return re.sub(r"\s+", " ", part)

    return {
        "field": item.field,
        "page": item.page,
        "before": ("…" if lo > 0 else "") + flat(text[lo:start]),
        "match": flat(text[start:end]),
        "after": flat(text[end:hi]) + ("…" if hi < len(text) else ""),
    }


def search(session: Session, query: str) -> list[tuple[Document, list[dict]]]:
    """Documents (not hidden ones) in which every word of `query` occurs, newest first, each with a few snippets. A word
    matches the document's title or its read text. At most MAX_RESULTS documents."""
    terms = terms_of(query)
    if not terms:
        return []
    ensure_indexed(session)
    docs = session.exec(select(Document).where(Document.ignored == False)).all()  # noqa: E712
    titles = {d.id: fold(nfc(d.title)) for d in docs}
    matching: set[int] | None = None
    for term in terms:
        in_body = set(session.exec(
            select(DocumentSearch.document_id).where(col(DocumentSearch.body).like(_like(term), escape="\\"))
        ).all())
        ids = {i for i, title in titles.items() if term in title} | in_body
        matching = ids if matching is None else matching & ids
        if not matching:
            return []
    hits = sorted((d for d in docs if d.id in matching), key=lambda d: (d.doc_date is not None, d.doc_date, d.id), reverse=True)
    results = []
    for doc in hits[:MAX_RESULTS]:
        report = session.get(DocumentReport, doc.id)
        pages = session.exec(select(DocumentText).where(DocumentText.document_id == doc.id).order_by(DocumentText.page)).all()
        snippets = []
        for item in [Field("title", doc.title), *fields(report, list(pages))]:
            snippet = _snippet(item, terms)
            if snippet:
                snippets.append(snippet)
            if len(snippets) == MAX_SNIPPETS:
                break
        results.append((doc, snippets))
    return results
