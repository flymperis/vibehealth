"""Text reports (imaging, medical opinions, prescriptions, and `other` documents read as narrative): what is kept, and
how it is shown.

Reading itself is in reading.py, the prompts and schemas in report_specs.py. Here: the checks on a page's text, the text
handed to the summary call, the clean-up and verification of the model's answer, and the queries the API needs.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from sqlalchemy import delete
from sqlmodel import Session, col, select

from .models import Document, DocumentKind, DocumentReport, DocumentSearch, DocumentText
from .textfold import fold, fold_words, nfc

MAX_FINDINGS = 8
MAX_CONCLUSION = 1500
MAX_FINDING = 300
LIST_CONCLUSION = 240  # how much of the conclusion a document list shows
RECENT_DAYS = 90  # the medication list covers prescriptions this recent (see current_medications)


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


def choose_route(texts: dict[int, str]) -> tuple[str, int, int]:
    """For a document of kind `other`: read it as "lab" or as a "report"? Returns (route, lab_pages, pages).
    Lab-like pages (looks_like_lab_page) dominate when they are at least half of the pages that have text. A tie,
    and a document with no text at all, go to "lab": that is what `other` always was."""
    pages = [t for t in texts.values() if t.strip()]
    lab = sum(1 for t in pages if looks_like_lab_page(t))
    return ("lab" if not pages or lab * 2 >= len(pages) else "report"), lab, len(pages)


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


def _text(value, limit: int) -> str:
    """A model's value as short single-line text. Anything but a string or a number is nothing."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())[:limit]


def _texts(value, count: int, limit: int) -> list[str]:
    """A list of distinct short strings (one string alone counts as a list of one)."""
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for raw in value if isinstance(value, list) else []:
        item = _text(raw, limit)
        if item and item not in out:
            out.append(item)
    return out[:count]


def clean_summary(result: dict) -> tuple[str, list[str]]:
    """The model's answer as stored: trimmed, short, no empty or repeated finding, at most MAX_FINDINGS."""
    return _text(result.get("conclusion"), MAX_CONCLUSION), _texts(result.get("key_findings"), MAX_FINDINGS, MAX_FINDING)


# --- what the model found, kind by kind ------------------------------------------------
# The model's answer is never trusted as it comes (report_specs.py says what is asked). Every field is optional and may
# be missing or of the wrong type: it then counts as empty. Claims that would do harm when wrong are checked against
# the text the reader transcribed: a medication field (one that is not in the text is dropped), a measurement's
# number and a date (they must be printed there).

MAX_MEASUREMENTS = 12
MAX_MEDICATIONS = 20
MED_FIELDS = ("name", "active_substance", "strength", "dose_instruction", "duration_or_quantity")

MODALITIES = ("ultrasound", "mri", "ct", "xray", "other")
_MODALITY_WORDS = (  # substrings of the folded answer, tried in this order
    ("mri", ("magnetic", "μαγνητικ")),
    ("ct", ("computed", "αξονικ", "cat scan")),
    ("xray", ("x-ray", "xray", "radiograph", "ακτινογραφ", "mammograph", "μαστογραφ")),
    ("ultrasound", ("ultrasound", "ultrasonic", "sonograph", "υπερηχ", "doppler", "triplex", "echo")),
)
_MODALITY_TOKENS = {"mri": "mri", "ct": "ct", "us": "ultrasound", "xr": "xray"}


def clean_modality(value) -> str:
    """One of MODALITIES, or "" when the answer is empty. Anything else that was said counts as "other"."""
    folded = fold_words(_text(value, 60))
    if not folded:
        return ""
    for token in re.split(r"[^\w-]+", folded):
        if token in _MODALITY_TOKENS:
            return _MODALITY_TOKENS[token]
    for modality, words in _MODALITY_WORDS:
        if any(w in folded for w in words):
            return modality
    return "other"


_MONTHS = (  # the start of a folded month name, Greek and English: the first that matches wins
    ("ιουν", 6), ("ιουλ", 7), ("ιαν", 1), ("φεβ", 2), ("μαρ", 3), ("απρ", 4), ("μαι", 5), ("αυγ", 8), ("σεπ", 9),
    ("οκτ", 10), ("νοε", 11), ("δεκ", 12), ("jan", 1), ("feb", 2), ("mar", 3), ("apr", 4), ("may", 5), ("jun", 6),
    ("jul", 7), ("aug", 8), ("sep", 9), ("oct", 10), ("nov", 11), ("dec", 12),
)
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4}|\d{2})(?!\d)")
_DATE_WORDS = re.compile(r"(?<!\d)(\d{1,2})(?:η|ου|ης|st|nd|rd|th)?\s+([^\W\d_]{3,})\.?,?\s+(\d{4})(?!\d)")


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day) if 1900 <= year <= 2100 else None
    except ValueError:
        return None


def parse_date(value) -> date | None:
    """A date from YYYY-MM-DD (what the model is asked for) or day/month/year (how Greek documents print it)."""
    text = _text(value, 20)
    found = _DATE_ISO.fullmatch(text)
    if found:
        return _make_date(int(found[1]), int(found[2]), int(found[3]))
    found = _DATE_DMY.fullmatch(text)
    if found:
        year = int(found[3])
        return _make_date(year + 2000 if year < 100 else year, int(found[2]), int(found[1]))
    return None


def dates_in_text(folded: str) -> set[date]:
    """The dates a (folded) text prints: 2025-03-12, 12/3/2025, 12.03.25, "12 Μαρτίου 2025", "12 March 2025"."""
    out = {_make_date(int(y), int(m), int(d)) for y, m, d in _DATE_ISO.findall(folded)}
    for d, m, y in _DATE_DMY.findall(folded):
        out.add(_make_date(int(y) + 2000 if len(y) == 2 else int(y), int(m), int(d)))
    for d, word, y in _DATE_WORDS.findall(folded):
        month = next((n for start, n in _MONTHS if word.startswith(start)), 0)
        if month:
            out.add(_make_date(int(y), month, int(d)))
    out.discard(None)
    return out  # type: ignore[return-value]


def _printed_date(value, folded: str) -> str:
    """The date as YYYY-MM-DD when the model gave one AND the text prints it; otherwise ""."""
    found = parse_date(value)
    return found.isoformat() if found and found in dates_in_text(folded) else ""


def _numbers(folded: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUM_RX.findall(folded)}


def _measurements(raw, folded: str) -> list[dict]:
    """[{label, value, unit}]: only where every number of the value is printed in the text."""
    printed: set[float] | None = None
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label, value, unit = _text(item.get("label"), 120), _text(item.get("value"), 40), _text(item.get("unit"), 20)
        numbers = _NUM_RX.findall(value)
        if not label or not numbers:
            continue
        printed = _numbers(folded) if printed is None else printed
        if not all(float(n.replace(",", ".")) in printed for n in numbers):
            continue  # "numbers only when explicit in the text"
        entry = {"label": label, "value": value, "unit": unit}
        if entry not in out:
            out.append(entry)
    return out[:MAX_MEASUREMENTS]


def _imaging(raw: dict, hay: str, folded: str) -> dict:
    return {
        "modality": clean_modality(raw.get("modality")),
        "regions": _texts(raw.get("regions"), 6, 80),
        "measurements": _measurements(raw.get("measurements"), folded),
    }


def _report(raw: dict, hay: str, folded: str) -> dict:
    follow = raw.get("follow_up") if isinstance(raw.get("follow_up"), dict) else {}
    return {
        "doctor": _text(raw.get("doctor"), 120),
        "specialty": _text(raw.get("specialty"), 80),
        "diagnoses": _texts(raw.get("diagnoses"), 8, 200),
        "recommendations": _texts(raw.get("recommendations"), 8, 300),
        "follow_up": {"text": _text(follow.get("text"), 200), "date": _printed_date(follow.get("date"), folded)},
    }


def _found(value: str, hay: str) -> bool:
    """`value` occurs in the text `hay` (both folded, white space made one space), as whole words or numbers, so
    that "5 mg" is not found in "25 mg"."""
    needle = fold_words(value)
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


def _medications(raw, hay: str) -> tuple[list[dict], int]:
    """(medications, how many entries were left out). A medication is copied, never inferred: a field whose text is
    not found in the transcribed text is dropped and named in `unverified` (the UI says the field was not
    confirmed). An entry with neither a name nor an active substance left is left out altogether."""
    meds: list[dict] = []
    left_out = 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        given = {f: _text(item.get(f), 200) for f in MED_FIELDS}
        med: dict = {}
        unverified: list[str] = []
        for field, value in given.items():
            if value and _found(value, hay):
                med[field] = value
            else:
                med[field] = ""
                if value:
                    unverified.append(field)
        if not (med["name"] or med["active_substance"]):
            left_out += any(given.values())
            continue
        med["unverified"] = unverified
        if med not in meds:
            meds.append(med)
    return meds[:MAX_MEDICATIONS], left_out + max(0, len(meds) - MAX_MEDICATIONS)


def _prescription(raw: dict, hay: str, folded: str) -> dict:
    meds, left_out = _medications(raw.get("medications"), hay)
    given = _text(raw.get("prescriber"), 120)
    prescriber = given if _found(given, hay) else ""
    when = _printed_date(raw.get("date"), folded)
    unverified = [f for f, wanted, kept in (("prescriber", given, prescriber), ("date", _text(raw.get("date"), 20), when))
                  if wanted and not kept]
    return {"prescriber": prescriber, "date": when, "medications": meds, "dropped_medications": left_out,
            "unverified": unverified}


_DETAILS = {DocumentKind.IMAGING: _imaging, DocumentKind.REPORT: _report, DocumentKind.PRESCRIPTION: _prescription}


def clean_details(kind: DocumentKind | str, raw: dict, texts: dict[int, str]) -> dict:
    """The kind-specific fields of the model's answer, checked against the page texts. {} for a kind without any."""
    build = _DETAILS.get(kind)  # type: ignore[call-overload]
    if build is None:
        return {}
    folded = " ".join(fold(nfc(t)) for _, t in sorted(texts.items()))
    return build(raw, " ".join(folded.split()), folded)


def has_content(details: dict) -> bool:
    """Something was found (the count of what was left out is not a finding)."""
    def full(value) -> bool:
        return any(full(v) for v in value.values()) if isinstance(value, dict) else bool(value)
    return any(full(v) for k, v in details.items() if k not in ("dropped_medications", "unverified"))


def clean(kind: DocumentKind | str, raw, texts: dict[int, str]) -> dict:
    """The model's answer for a text report of `kind`, ready to store: {status, error, conclusion, key_findings,
    details}. It never raises on a strange answer: what cannot be used is left out."""
    raw = raw if isinstance(raw, dict) else {}
    conclusion, findings = clean_summary(raw)
    details = clean_details(kind, raw, texts)
    found = bool(conclusion or findings) or has_content(details)
    return {"status": "ok" if found else "empty", "error": "", "conclusion": conclusion,
            "key_findings": findings, "details": details}


def forget(session: Session, document_id: int) -> None:
    """Remove what a reading as a text report left (page text, summary, search text) for a document that is now read
    as a lab report. The caller commits."""
    for table in (DocumentText, DocumentSearch, DocumentReport):
        session.execute(delete(table).where(col(table.document_id) == document_id))


# --- for the API -----------------------------------------------------------------------


def _json(value: str, kind: type):
    try:
        data = json.loads(value or "")
    except ValueError:
        return kind()
    return data if isinstance(data, kind) else kind()


def _findings(report: DocumentReport) -> list[str]:
    return [f for f in _json(report.key_findings, list) if isinstance(f, str)]


def _pages(report: DocumentReport) -> list[int]:
    return [n for n in _json(report.lab_pages, list) if isinstance(n, int)]


def details_of(report: DocumentReport) -> dict:
    """The kind-specific fields as stored; {} for a summary written before they existed (or a damaged one)."""
    return _json(report.details, dict)


def _strings(value) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _brief(report: DocumentReport) -> dict:
    """What a document list shows besides the conclusion: a modality, regions, a first diagnosis, a medication count."""
    d = details_of(report)
    diagnoses = _strings(d.get("diagnoses"))
    return {
        "modality": d.get("modality") if d.get("modality") in MODALITIES else "",
        "regions": _strings(d.get("regions"))[:2],
        "diagnosis": diagnoses[0] if diagnoses else "",
        "medications": len(d["medications"]) if isinstance(d.get("medications"), list) else 0,
    }


def summaries(session: Session, document_ids: list[int]) -> dict[int, dict]:
    """Per document that has been read as a text report: how the summary went, how many findings it has, the
    start of its conclusion and a few kind-specific facts (what the document lists show)."""
    if not document_ids:
        return {}
    rows = session.exec(select(DocumentReport).where(col(DocumentReport.document_id).in_(document_ids))).all()
    return {
        r.document_id: {"status": r.summary_status, "findings": len(_findings(r)),
                        "conclusion": r.conclusion[:LIST_CONCLUSION], **_brief(r)}
        for r in rows
    }


def detail(session: Session, document_id: int) -> dict | None:
    """The summary, the kind-specific fields, the findings, the page text and the lab-looking pages of one document;
    None when it has not been read as a text report."""
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
        "details": details_of(report),
        "lab_pages": _pages(report),
        "updated_at": report.updated_at.isoformat(timespec="seconds"),
        "pages": [{"page": p.page, "text": p.text} for p in pages],
    }


def _s(value) -> str:
    return value if isinstance(value, str) else ""


def current_medications(session: Session, today: date | None = None) -> dict:
    """The medications of the prescriptions of the last RECENT_DAYS days, one entry per medicine (grouped by name,
    accents and case ignored) taken from the newest prescription that has it. An AUTOMATIC list, not a treatment
    plan: it only holds what was read from the documents, and only what the text confirmed (see _medications).
    A prescription's date is the document's date, else the date read from it; one with neither is only counted
    (`undated`), and one older than the window is counted in `older`."""
    today = today or date.today()
    limit = today - timedelta(days=RECENT_DAYS)
    rows = session.exec(
        select(DocumentReport, Document)
        .join(Document, col(DocumentReport.document_id) == col(Document.id))
        .where(Document.kind == DocumentKind.PRESCRIPTION, Document.ignored == False)  # noqa: E712
    ).all()
    groups: dict[str, list[tuple[date, Document, dict]]] = {}
    undated = older = 0
    for report, doc in rows:
        details = details_of(report)
        meds = [m for m in details["medications"] if isinstance(m, dict)] if isinstance(
            details.get("medications"), list) else []
        if not meds:
            continue
        when = doc.doc_date or parse_date(details.get("date"))
        if when is None:
            undated += 1
        elif when < limit:
            older += 1
        else:
            for med in meds:
                label = _s(med.get("name")) or _s(med.get("active_substance"))
                if label:
                    groups.setdefault(fold_words(label), []).append((when, doc, med))
    medications = []
    for entries in groups.values():
        entries.sort(key=lambda e: (e[0], e[1].id), reverse=True)
        when, doc, med = entries[0]
        medications.append({
            **{f: _s(med.get(f)) for f in MED_FIELDS},
            "unverified": _strings(med.get("unverified")),
            "date": when.isoformat(),
            "document_id": doc.id,
            "document_title": doc.title,
            "earlier_dates": sorted({e[0].isoformat() for e in entries[1:]}, reverse=True),
        })
    medications.sort(key=lambda m: fold_words(m["name"] or m["active_substance"]))
    medications.sort(key=lambda m: m["date"], reverse=True)  # (stable: the newest first, then by name)
    return {"days": RECENT_DAYS, "medications": medications, "undated": undated, "older": older}
