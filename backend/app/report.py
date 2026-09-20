"""Text reports (imaging, medical opinions, prescriptions): what is kept, and how it is shown.

Reading itself is in reading.py. Here: the checks on a page's text, the text handed to the summary call,
the clean-up of the model's answer and the queries the API needs.
"""

from __future__ import annotations

import json
import re

from sqlmodel import Session, col, select

from .models import DocumentKind, DocumentReport, DocumentText

# What the summary prompt calls each kind of document.
KIND_NAMES = {
    DocumentKind.IMAGING: "imaging report",
    DocumentKind.REPORT: "medical opinion",
    DocumentKind.PRESCRIPTION: "prescription",
}
MAX_FINDINGS = 8
MAX_CONCLUSION = 1500
MAX_FINDING = 300
LIST_CONCLUSION = 240  # how much of the conclusion a document list shows


# --- a lab table inside a report --------------------------------------------------
# A PDF can hold a page of lab results inside an imaging report. A page "looks like lab results" when
# several of its lines carry a number, a unit and a reference range, as a results table does.

LAB_LINES_MIN = 4

_NUM = r"\d+(?:[.,]\d+)?"
_RANGE_RX = re.compile(rf"{_NUM}\s*[-–—]\s*{_NUM}|[<>≤≥]=?\s*{_NUM}")
_NUM_RX = re.compile(_NUM)
# %, fL, pg, or a ratio such as g/dL, mmol/l, U/L, mm/h, 10^3/uL, x10^3/uL (Latin and Greek mu)
_UNIT_RX = re.compile(
    r"%|\b(?:fl|pg)\b|(?:[x×]?\s?10\^?\d+|[a-zµμ]{1,6})/[a-zµμ]{1,4}\d?(?![a-z0-9])",
    re.IGNORECASE,
)


def _lab_line(line: str) -> bool:
    found = _RANGE_RX.search(line)
    if not found:
        return False
    rest = line[: found.start()] + " " + line[found.end():]  # a number besides the range itself: the result
    return bool(_NUM_RX.search(rest)) and bool(_UNIT_RX.search(line))


def looks_like_lab_page(text: str) -> bool:
    """True when at least LAB_LINES_MIN lines hold a value, a unit and a reference range."""
    return sum(1 for line in text.splitlines() if _lab_line(line)) >= LAB_LINES_MIN


# --- the summary ---------------------------------------------------------------------


def summary_input(texts: dict[int, str], limit: int) -> str:
    """The pages' text as one string of at most about `limit` characters (a token is at least a character,
    so the prompt and the answer still fit in the context). A longer report keeps its start and, more
    important, its end, where the conclusion usually is."""
    joined = "\n\n".join(f"[page {n}]\n{text}" for n, text in sorted(texts.items()))
    if len(joined) <= limit:
        return joined
    head = int(limit * 0.6)
    return joined[:head] + "\n[...]\n" + joined[len(joined) - (limit - head):]


def clean_summary(result: dict) -> tuple[str, list[str]]:
    """The model's answer as stored: trimmed, short, no empty or repeated finding, at most MAX_FINDINGS."""
    conclusion = " ".join(str(result.get("conclusion", "")).split())[:MAX_CONCLUSION]
    findings: list[str] = []
    for raw in result.get("key_findings", []):
        finding = " ".join(str(raw).split())[:MAX_FINDING]
        if finding and finding not in findings:
            findings.append(finding)
    return conclusion, findings[:MAX_FINDINGS]


# --- for the API -----------------------------------------------------------------------


def _findings(report: DocumentReport) -> list[str]:
    try:
        value = json.loads(report.key_findings or "[]")
    except ValueError:
        return []
    return [f for f in value if isinstance(f, str)] if isinstance(value, list) else []


def _pages(report: DocumentReport) -> list[int]:
    try:
        value = json.loads(report.lab_pages or "[]")
    except ValueError:
        return []
    return [n for n in value if isinstance(n, int)] if isinstance(value, list) else []


def summaries(session: Session, document_ids: list[int]) -> dict[int, dict]:
    """Per document that has been read as a text report: how the summary went, how many findings it has and the
    start of its conclusion (what the document lists show)."""
    if not document_ids:
        return {}
    rows = session.exec(select(DocumentReport).where(col(DocumentReport.document_id).in_(document_ids))).all()
    return {
        r.document_id: {"status": r.summary_status, "findings": len(_findings(r)), "conclusion": r.conclusion[:LIST_CONCLUSION]}
        for r in rows
    }


def detail(session: Session, document_id: int) -> dict | None:
    """The summary, the findings, the page text and the lab-looking pages of one document; None when it
    has not been read as a text report."""
    report = session.get(DocumentReport, document_id)
    if report is None:
        return None
    pages = session.exec(
        select(DocumentText).where(DocumentText.document_id == document_id).order_by(DocumentText.page)
    ).all()
    return {
        "auto": report.auto_generated,  # written by a model: the original prevails
        "summary_status": report.summary_status,
        "summary_error": report.summary_error,
        "summary_model": report.summary_model,
        "conclusion": report.conclusion,
        "key_findings": _findings(report),
        "lab_pages": _pages(report),
        "updated_at": report.updated_at.isoformat(timespec="seconds"),
        "pages": [{"page": p.page, "text": p.text} for p in pages],
    }
