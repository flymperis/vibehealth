"""Combine the two readers and the Paperless text into one value per test.

The rules are the ones measured in the extraction benchmark (score.py /
score_kosmo.py, "STRICT" policy):

- A value is **verified** when both readers read the same value, or when one
  reader's value appears on a line of the Paperless OCR text that itself maps
  to the same test (so "CHOL/HDL 4.2" can never confirm HDL = 4.2), and, for a
  text value, the line has no other digits (so "pH Όξινη 5.5" cannot confirm
  "Όξινη").
- Everything else **needs review**, with a short reason.

Only the first occurrence of a test in a document counts: later pages of a lab
report repeat older results in a history table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import (
    TWIN_OF,
    flag_for,
    is_empty_value,
    map_code,
    norm_value,
    skel,
    strip_accents,
)

VERIFIED = "verified"
NEEDS_REVIEW = "needs_review"


@dataclass
class ReaderRow:
    name: str
    value: str
    unit: str = ""
    reference_range: str = ""
    page: int = 0
    code: str | None = None


@dataclass
class Candidate:
    test_code: str | None
    raw_name: str
    value: str
    unit: str
    ref_range: str
    flag: str
    status: str
    reason: str
    page: int | None
    reader_a: str | None = None
    reader_b: str | None = None
    extra: dict = field(default_factory=dict)


def first_occurrences(rows: list[ReaderRow]) -> dict[str, ReaderRow]:
    """First row per test in page order. Rows without a test are keyed by their name."""
    out: dict[str, ReaderRow] = {}
    for row in sorted(rows, key=lambda r: r.page):  # stable: keeps reading order within a page
        if is_empty_value(row.value):
            continue
        code = row.code or map_code(row.name, row.value, row.unit)
        row.code = code
        key = code or "?" + skel(row.name)
        if key != "?" and key not in out:
            out[key] = row
    return out


def text_lines(content: str | None) -> list[str]:
    return [line for line in (content or "").splitlines() if line.strip()]


def value_in_line(value: str, line: str) -> bool:
    nv = norm_value(value)
    if nv[0] == "n":
        s = re.sub(r"^[<>]\s*", "", value.strip().replace(",", "."))
        alts = {s, s.replace(".", ",")}
        return any(re.search(r"(?<![\d.,])" + re.escape(a) + r"(?![\d])", line) for a in alts)
    t = nv[1]
    lt = strip_accents(line.lower()).replace(" ", "").translate(str.maketrans({"o": "ο", "x": "χ", "i": "ι"}))
    # a text value only confirms when the line has no other digits
    return bool(t) and t in lt and not re.search(r"\d", lt.replace(t, ""))


def text_confirms(code: str, value: str, lines: list[str]) -> bool:
    """STRICT: some line holds the value and that line's own test is this test."""
    base = code.replace("_ABS", "")
    # A numeric urine value maps like the blood test ("Σάκχαρο 100" -> GLU), so the twin counts too.
    wanted = {base, TWIN_OF.get(base)} - {None}
    for line in lines:
        if value_in_line(value, line) and (map_code(line, value, "") or "").replace("_ABS", "") in wanted:
            return True
    return False


def _candidate(code, row: ReaderRow, status, reason, a, b, other: ReaderRow | None = None) -> Candidate:
    unit = row.unit or (other.unit if other else "")
    rng = row.reference_range or (other.reference_range if other else "")
    return Candidate(
        test_code=code,
        raw_name=row.name,
        value=row.value.strip(),
        unit=unit.strip(),
        ref_range=rng.strip(),
        flag=flag_for(row.value, rng),
        status=status,
        reason=reason,
        page=row.page or None,
        reader_a=a.value.strip() if a else None,
        reader_b=b.value.strip() if b else None,
    )


def combine(
    rows_a: list[ReaderRow],
    rows_b: list[ReaderRow] | None,
    lines: list[str] | None,
    reader_b_on: bool = True,
) -> list[Candidate]:
    """One candidate per test (plus unknown names from reader A, for review)."""
    first_a = first_occurrences(rows_a)
    first_b = first_occurrences(rows_b or [])
    lines = lines or []
    out: list[Candidate] = []

    for key in list(dict.fromkeys([*first_a, *first_b])):
        a, b = first_a.get(key), first_b.get(key)
        if key.startswith("?"):
            row = a or b
            out.append(_candidate(None, row, NEEDS_REVIEW, "unknown test name", a, b))
            continue
        code = key
        if a and b and norm_value(a.value) == norm_value(b.value):
            out.append(_candidate(code, a, VERIFIED, "both readers agree", a, b, other=b))
            continue
        ok_a = bool(a) and text_confirms(code, a.value, lines)
        ok_b = bool(b) and text_confirms(code, b.value, lines)
        if a and b:
            if ok_a and not ok_b:
                out.append(_candidate(code, a, VERIFIED, "readers differ; Paperless text matches reader A", a, b, b))
            elif ok_b and not ok_a:
                out.append(_candidate(code, b, VERIFIED, "readers differ; Paperless text matches reader B", a, b, a))
            else:
                out.append(_candidate(code, a, NEEDS_REVIEW, "readers differ", a, b, b))
            continue
        row, only = (a, "A") if a else (b, "B")
        if (ok_a if a else ok_b):
            out.append(_candidate(code, row, VERIFIED, f"reader {only} and Paperless text agree", a, b))
        elif only == "A" and not reader_b_on:
            out.append(_candidate(code, row, NEEDS_REVIEW, "single reader, not in Paperless text", a, b))
        else:
            out.append(_candidate(code, row, NEEDS_REVIEW, f"only reader {only} found it", a, b))
    return out
