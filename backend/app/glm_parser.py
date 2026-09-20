"""Turn glm-ocr text (markdown / HTML tables / dotted-leader lines) into rows.

Ported from the extraction benchmark (see docs/DESIGN.md),
including the layout fixes for a second laboratory's report format. Each row is
`{name, value, unit, reference_range, code}`; lines that map to no known test
are dropped. The only change from the benchmark is the fuzzy fallback: it
matches against the catalogue's Greek names instead of the benchmark's ground
truth.
"""

from __future__ import annotations

import difflib
import html
import re
from functools import lru_cache

from . import catalog
from .catalog import CYR, URINE_TWINS, is_qual, map_code, skel

DIFF_CODES = ["NEUT", "LYMPH", "MONO", "EOS", "BASO"]
RANGE_RX = r"(?:[<>]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*-\s*\d+(?:[.,]\d+)?)"
# absolute-count unit: '10^3/μl' (one laboratory), 'K/μl' (another)
ABS_TAIL = re.compile(
    r"^(" + RANGE_RX + r")\s+(\d+(?:[.,]\d+)?)\s+(10\s*\^\s*\d\s*/\s*\S+|[KkКΚ]\s*/\s*\S+)\s+("
    + RANGE_RX + r")\s*$"
)
KOP = r"[κkк]\.?\s?[οoо]\.?\s?[πηпn]\.?"


@lru_cache(maxsize=1)
def _vocab() -> list[tuple[str, str]]:
    return catalog.fuzzy_vocabulary()


def _latin_cyrillic(text: str) -> str:
    return "".join(ch.translate(CYR) if "Ѐ" <= ch <= "ӿ" else ch for ch in text)


def fuzzy(name: str, restrict: list[str] | None = None) -> str | None:
    s = skel(re.sub(r"\(.*?\)", "", name))
    s = re.sub(r"[^a-z0-9 ]", "", s).strip()
    if len(s) < 3:
        return None
    best, best_code = 0.0, None
    for code, vocab in _vocab():
        if restrict and code not in restrict:
            continue
        ratio = difflib.SequenceMatcher(None, s, vocab).ratio()
        if ratio > best:
            best, best_code = ratio, code
    return best_code if best >= (0.5 if restrict else 0.72) else None


def code_for(name: str, value: str, unit: str, restrict: list[str] | None = None) -> str | None:
    code = map_code(name, value, unit)
    if code and (not restrict or code in restrict or code.replace("_ABS", "") in restrict):
        return code
    code = fuzzy(name, restrict)
    if code in URINE_TWINS and is_qual(value):  # same twin rule as map_code
        code = URINE_TWINS[code]
    return code


def clean(t: str) -> str:
    t = html.unescape(t)
    t = re.sub(r"\$\s*\^\{+\*\}+\s*\$", "*", t)
    t = re.sub(r"\$([^$]*)\$", lambda m: " " + re.sub(r"\s+", "", m.group(1)) + " ", t)
    t = t.replace("\\mu", "μ").replace("\\Sigma", "Σ").replace("µ", "μ")
    t = re.sub(r"μ\s+(?=[A-Za-zл])", "μ", t)
    t = re.sub(r"\^\{+(\w+)\}+", r"^\1", t)
    t = t.replace("$", "").replace("{", "").replace("}", "")
    t = re.sub(r"(?<!\w)['’](?=[Α-ΩΆΈΉΊΌΎΏ])", "", t)  # "'Οχι": apostrophe used as capital tonos
    return re.sub(r"\s+", " ", t).strip()


def split_value(rest: str, allow_text: bool) -> tuple[str, str, str] | None:
    """'value [unit] [range]' -> (value, unit, range), or None."""
    rest = re.sub(r"\bT\.?\s?A\.?\s*:?", " ", rest).strip()
    m = re.match(r"^(\d+\s*-\s*\d+)\s*(" + KOP + r")\s*$", rest)
    if m:
        return m.group(1), m.group(2), ""
    m = re.match(r"^([<>]?\s*\d+(?:[.,]\d+)?)(?:\s+(.*))?$", rest)
    if m:
        value, tail = m.group(1), (m.group(2) or "").strip()
        unit = ""
        um = re.match(r"^(10\s*\^\s*\d\s*/\s*\S+|[^\d<>\s][^\s]*)\s*(.*)$", tail)
        if um and not re.match(r"^(Επιθ|Έλλ|Ελλ)", tail):
            unit, tail = um.group(1), um.group(2)
        else:  # urine layout of the second laboratory: unit printed after the range ("0.2 - 1 E.U./dl")
            rm = re.match(r"^(" + RANGE_RX + r")\s+([^\d\s<>][^\s]*)$", tail)
            if rm:
                tail, unit = rm.group(1), rm.group(2)
        return value, _latin_cyrillic(unit).replace(" ", ""), tail.strip()
    if allow_text:
        rest = re.sub(r"\s*[\"”″]+\s*$", "", rest)  # trailing ditto mark in the range column
        m = re.match(r"^([^\d\s:][^\s:]*)\s*$", rest)
        if m and len(m.group(1)) <= 20 and re.search(r"[^\W\d_]|\+", m.group(1)):
            return m.group(1), "", ""
        # urine layout of the second laboratory: 'value  range' on one line; the range may be a ditto
        # mark ("), the value may be 'Σπάνια 0 - 3 κ.ο.π.'
        m = re.match(
            r"^([^\d\W][^\s:\"]*(?:\s+\d+\s*-\s*\d+\s*" + KOP + r"|\s+\d+(?:[.,]\d+)?(?!\s*-))?)\s*(.*)$",
            rest,
        )
        # empty morphology grids 'Name ..... Name2 ..... Name3 .....' are not 'value range'
        if m and len(m.group(1)) <= 30 and ":" not in m.group(2) and not re.search(r"\.{3,}|…", m.group(2)):
            rng = m.group(2).strip()
            if re.fullmatch(r"[\"'”″]+", rng):
                rng = ""
            if len(rng.split()) <= 6:
                return m.group(1), "", rng
    return None


def _handle(name: str, rest: str, rows: list[dict], allow_text: bool) -> bool:
    """name + 'value [unit] [range] [abs-value abs-unit abs-range]' -> rows."""
    if not name or re.search(r"\d{2}/\d{2}/\d{4}|www\.|@|amka|σελίδα|εντολής", name.lower() + rest.lower()):
        return False
    if re.search(r"[\s.]\s*(mm|%)$", name):  # empty result: unit printed before the range
        return False
    sv = split_value(rest, allow_text=allow_text)
    if not sv:
        return False
    value, unit, rng = sv
    am = ABS_TAIL.match(rng) if unit == "%" else None
    if am:  # white-cell differential with the absolute count on the same row
        code = code_for(name, value, unit, DIFF_CODES)
        if not code:
            return False
        base = code.replace("_ABS", "")
        rows.append(dict(name=name, value=value, unit="%", reference_range=am.group(1), code=base))
        rows.append(dict(
            name=name + " (απόλυτος αριθμός)", value=am.group(2),
            unit=_latin_cyrillic(am.group(3)).replace(" ", ""),
            reference_range=am.group(4), code=base + "_ABS",
        ))
        return True
    code = code_for(name, value, unit)
    if not code:
        return False
    rows.append(dict(name=name, value=value, unit=unit, reference_range=rng, code=code))
    return True


def _add_pair(text: str, rows: list[dict]) -> None:
    text = clean(text)
    # 1) "name [dots|colon] <number> ..." (first numeric token after the name)
    # 2) "name ..... : value" / "name ..... value"
    m = re.match(r"^(.*?[^\s.…·:])[\s.…·:]+([<>]?\s*\d+(?:[.,]\d+)?(?:\s.*)?)$", text)
    if m and re.search(r"[.…·]{2,}\s*:", m.group(1)):
        m = None  # 'Name ..... : Σπάνια 0 - 3': the number belongs to a text value
    m = (
        m
        or re.match(r"^(.*?)[\s.…·]*:\s*(.+)$", text)
        or re.match(r"^(.*?[^\s.…·])[\s.…·]{3,}\s*(\S.*)$", text)
    )
    if not m:
        return
    _handle(m.group(1).strip(" *"), m.group(2).strip(), rows, allow_text=True)


def _rows_from_cells(cells: list[str], rows: list[dict]) -> None:
    cells = [clean(c) for c in cells]
    # colon-form cells ("Name ..... : value"), e.g. the urine layout
    if all(re.search(r":\s*\S", c) or not c for c in cells) and any(cells):
        for c in cells:
            _add_pair(c, rows)
        return
    if len(cells) < 2:
        if cells:
            _add_pair(cells[0], rows)
        return
    name = re.sub(r"[\s.…·]*:?\s*$", "", cells[0]).strip(" *")
    rest = " ".join(c for c in cells[1:] if c)
    _handle(name, rest, rows, allow_text=False)


def parse(text: str) -> list[dict]:
    rows: list[dict] = []
    text = (text or "").replace("\r", "")
    pos = 0
    for tm in re.finditer(r"<table.*?</table>", text, re.S):
        for line in text[pos:tm.start()].split("\n"):
            _add_pair(line, rows)
        for tr in re.findall(r"<tr>(.*?)</tr>", tm.group(0), re.S):
            _rows_from_cells(re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S), rows)
        pos = tm.end()
    for line in text[pos:].split("\n"):
        line = line.strip()
        if line.startswith("|"):  # markdown table
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            _rows_from_cells(cells, rows)
        else:
            _add_pair(line, rows)
    return rows


def truncated(text: str) -> bool:
    """glm-ocr sometimes stops mid-table on a dotted leader line (EOS too early)."""
    return bool(re.search(r"(\. ){5,}\.?$|\.{6,}$", (text or "").rstrip()))
