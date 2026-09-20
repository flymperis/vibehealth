"""Which models the project has tested, and the "model check": try any installed model on a sample file.

Two things live here.

1. The registry of models the project measured (`KNOWN`), and `model_info` which describes an installed model
   for the Settings page: tested / untested / not recommended / not a vision model.

2. The check itself. A person picks an installed model, sends a sample file (a page of a lab report they know)
   and, optionally, the values they know are on it. The server reads the file page by page with the same
   sandbox rendering the readers use, runs reader A (and reader B if asked) and reports what came out. Nothing
   is saved but counts: no document, value or run row is made, the file is removed at the end (also on an
   error) and the expected values are used in memory only. The counts of the last checks are kept in the
   settings store (`reading.model_checks`) so the Settings page can show them next to each model.

One check at a time, never while a document is being read, and the reading queue waits while one runs (they
would fight for the GPU and each other's timings). It runs as a background task with progress in
`worker.check_state`, under a hard timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlmodel import Session

from . import reading_settings, uploads
from .catalog import BY_CODE, map_code, norm_value, skel
from .db import engine
from .glm_parser import parse as parse_glm
from .models import AppSetting
from .ollama import Ollama, OllamaError, PageError, SafeError, StructuredOutputError, describe, has_model
from .reading import _read_b
from .reading_settings import MODEL_RX
from .sandbox import Refused, SandboxError, SandboxPages
from .settings_store import put
from .verify import VERIFIED, ReaderRow, combine, first_occurrences
from .worker import check_state, state

log = logging.getLogger("vibehealth")

CHECKS_KEY = "reading.model_checks"
KEEP_MODELS = 10  # how many models' last checks are remembered
MAX_PAGES = 5  # a check reads at most this many pages of the file
HARD_TIMEOUT = 15 * 60  # seconds for the whole check
MAX_EXPECTED_LINES = 400
MAX_EXPECTED_LINE_CHARS = 200  # a longer line is skipped (it is a mistake or an attempt to make the parser work hard)
# The form fields of the request and how long each may be (bytes): see uploads.receive.
TEXT_FIELDS = {"model": 512, "use_reader_b": 64, "expected": 16 * 1024}


# --- the models the project has tested -------------------------------------------------------------------


@dataclass(frozen=True)
class Known:
    key: str  # what the UI keys its translated note on
    pattern: re.Pattern
    status: str  # "tested" | "not_recommended"
    role: str | None  # "reader_a" | "reader_b" | None
    note: str  # English; the UI has its own translations, keyed by `key`


KNOWN: tuple[Known, ...] = (
    Known(
        "qwen3.5-4b", re.compile(r"qwen3\.5:4b"), "tested", "reader_a",
        "Reader A. Measured: 128 of 129 Greek lab values correct at 150 dpi on two labs, about 12 s per "
        "results page on an RTX 2070, fully on the GPU.",
    ),
    Known(
        "glm-ocr", re.compile(r"glm-ocr(:.+)?"), "tested", "reader_b",
        "Reader B (the second reader). Needs its retry rule for cut-off pages; the text parser is built "
        "for its output.",
    ),
    Known(
        "qwen3-vl", re.compile(r"qwen3-vl:4b.*"), "not_recommended", None,
        "Measured, not recommended: the 4b instruct model invented more rows.",
    ),
    Known(
        "paddleocr-vl", re.compile(r".*paddleocr.*", re.I), "not_recommended", None,
        "Measured, not recommended: rows silently shifted, and it needs a lot of memory.",
    ),
)
UNTESTED_NOTE = "Not tested by the project: run the model check with a file you know before trusting its values."
NO_VISION_NOTE = "This model does not accept images, so it cannot read pages."
_GLM_LIKE = re.compile(r"glm[-_. ]?ocr", re.I)


def glm_like(name: str) -> bool:
    """Is this glm-ocr (or a rename of it)? Reader B's parser is tied to that model's output."""
    return bool(_GLM_LIKE.search(name))


def model_info(name: str, capabilities: list[str] | None) -> dict:
    """{name, tested, status, role, note, key, vision, usable, glm_like} for one installed model.

    `vision` is what Ollama says (`capabilities`), None when it does not say. A model that says it has no vision
    is `no_vision` and not usable, whatever the project measured."""
    vision = None if capabilities is None else "vision" in capabilities
    known = next((k for k in KNOWN if k.pattern.fullmatch(name)), None)
    if vision is False:
        status, note, key = "no_vision", NO_VISION_NOTE, "no_vision"
    elif known:
        status, note, key = known.status, known.note, known.key
    else:
        status, note, key = "untested", UNTESTED_NOTE, "untested"
    return {
        "name": name,
        "tested": known is not None and vision is not False,
        "status": status,
        "role": known.role if known and vision is not False else None,
        "note": note,
        "key": key,
        "vision": vision,
        "usable": vision is not False,
        "glm_like": glm_like(name),
    }


# --- the values the person knows are on the page ----------------------------------------------------------


@dataclass
class Expected:
    key: str  # a test code, or "?" + the skeleton of a name the catalogue does not know
    value: str


_NUMBER = re.compile(r"([<>]?\s*-?\d+(?:[.,]\d+)?)(?:\s+(.*))?", re.S)
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,15}")


def parse_expected(text: str) -> tuple[list[Expected], list[str]]:
    """One known value per line: `Name = value unit` or `CODE: value` (a decimal comma is fine). Names go
    through the catalogue like the readers' rows do, so `Ουρία`, `urea` written as the lab printed it and
    the code `UREA` all mean the same test. A name the catalogue does not know still matches a row whose
    printed name looks the same. Returns the entries and warnings that name a LINE NUMBER, never its text."""
    entries: list[Expected] = []
    warnings: list[str] = []
    seen: set[str] = set()
    lines = (text or "").replace(chr(160), " ").splitlines()
    if len(lines) > MAX_EXPECTED_LINES:
        warnings.append(f"only the first {MAX_EXPECTED_LINES} expected lines were used")
        lines = lines[:MAX_EXPECTED_LINES]
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_EXPECTED_LINE_CHARS:
            warnings.append(f"expected line {number} was skipped: longer than {MAX_EXPECTED_LINE_CHARS} characters")
            continue
        sep = "=" if "=" in line else ":" if ":" in line else ""
        left, _, right = line.partition(sep) if sep else (line, "", "")
        left, right = left.strip(), right.strip()
        if not left or not right:
            warnings.append(f"expected line {number} was skipped: use Name = value unit, or CODE: value")
            continue
        found = _NUMBER.fullmatch(right)
        value, unit = (found.group(1).strip(), (found.group(2) or "").strip()) if found else (right, "")
        code = left if _CODE.fullmatch(left) and left in BY_CODE else map_code(left, value, unit)
        key = code or "?" + skel(left)
        if key == "?":
            warnings.append(f"expected line {number} was skipped: no test name")
            continue
        if key in seen:
            warnings.append(f"expected line {number} names a test that is already listed: only the first counts")
            continue
        seen.add(key)
        entries.append(Expected(key, value))
    return entries, warnings


def score(rows_a: list[ReaderRow], expected: list[Expected]) -> dict:
    """Compare reader A's rows (first occurrence per test, as the pipeline keeps them) with what is known."""
    first = first_occurrences(rows_a)
    matched = wrong = missing = 0
    for item in expected:
        row = first.get(item.key)
        if row is None:
            missing += 1
        elif norm_value(row.value) == norm_value(item.value):
            matched += 1
        else:
            wrong += 1
    listed = {item.key for item in expected}
    return {"matched": matched, "wrong_value": wrong, "missing": missing,
            "extra": sum(1 for key in first if key not in listed)}


# --- the run -----------------------------------------------------------------------------------------------


class CheckBusy(Exception):
    """A reading or another check is running (the message is ours and safe to show)."""


class CheckError(SafeError):
    """The check could not be done; the message is ours."""


@dataclass
class Plan:
    kind: str  # the file type from its bytes: pdf | jpeg | png | webp
    model: str  # as Ollama lists it
    use_reader_b: bool
    expected: list[Expected]
    notes: list[str] = field(default_factory=list)  # warnings from reading the expected list


def begin() -> dict:
    """Claim the one place for a check, or raise CheckBusy. Returns the progress dict."""
    if check_state["current"] is not None:
        raise CheckBusy("Another model check is already running.")
    reading = state["reading"]
    if reading["current"] is not None or reading["queue"]:
        raise CheckBusy("Documents are being read right now: run the model check when the reading is done.")
    progress = {"model": "", "stage": "receiving", "page": 0, "pages": 0,
                "started_at": datetime.now().isoformat(timespec="seconds")}
    check_state["current"] = progress
    return progress


def release() -> None:
    check_state["current"] = None


def _flag(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("", "0", "false", "no", "off"):
        return False
    raise uploads.UploadError(422, "use_reader_b must be true or false")


async def prepare(received: uploads.Received) -> Plan:
    """Everything that can be refused before the check starts (the file has arrived in uploads/.tmp): its type
    from the bytes, the form fields, the file opened by the sandbox, and the model, which must be one that
    Ollama lists and that can see images. The model name that is used is the one from Ollama's list."""
    fields = received.fields
    kind = uploads.sniff(received.head)
    if kind is None:
        raise uploads.UploadError(415, f"only {uploads.ACCEPTED} files are accepted")
    wanted = fields.get("model", "").strip()
    if not MODEL_RX.fullmatch(wanted):
        raise uploads.UploadError(422, "Choose one of the installed models.")
    use_b = _flag(fields.get("use_reader_b", ""))
    expected, notes = parse_expected(fields.get("expected", ""))

    await uploads.check(received.path, kind)  # opened in the sandbox, like an upload

    settings = reading_settings.load()
    client = Ollama(settings.ollama_url, settings.timeout_seconds, settings.num_ctx, settings.keep_alive)
    try:
        installed = await client.models()
    except OllamaError as exc:
        raise uploads.UploadError(502, describe(exc)) from None
    model = next((m for m in installed if m == wanted or (":" not in wanted and m == f"{wanted}:latest")), None)
    if model is None:
        raise uploads.UploadError(422, "That model is not installed in Ollama.")
    if not model_info(model, await client.capabilities(model))["usable"]:
        raise uploads.UploadError(422, NO_VISION_NOTE + " Choose a vision model.")
    if use_b and not has_model(installed, settings.reader_b_model):
        raise uploads.UploadError(
            422, f"Reader B's model ({settings.reader_b_model}) is not installed in Ollama: pull it, or run the check without it."
        )
    return Plan(kind, model, use_b, expected, notes)


async def run_check(path: str, plan: Plan, progress: dict) -> dict:
    """Read the file page by page and describe what came out. No database rows, no file kept."""
    settings = reading_settings.load()
    client = Ollama(settings.ollama_url, settings.timeout_seconds, settings.num_ctx, settings.keep_alive)
    warnings = list(plan.notes)
    progress.update(model=plan.model, stage="rendering")
    try:
        pages = await asyncio.to_thread(SandboxPages, path, plan.kind)
    except (SandboxError, Refused) as exc:
        raise CheckError("The file could not be read: " + describe(exc)) from None
    try:
        total = len(pages)
        count = min(total, MAX_PAGES)
        progress.update(pages=count)
        if total > count:
            warnings.append(f"only the first {count} of {total} pages were checked")

        rows_a: list[ReaderRow] = []
        images: dict[int, bytes] = {}
        seconds = 0.0
        read_pages = structured_failures = 0
        for i in range(count):
            progress.update(stage="reader_a", page=i + 1)
            try:
                images[i] = await asyncio.to_thread(pages.png, i, settings.dpi)
            except (SandboxError, Refused):
                warnings.append(f"page {i + 1}: the page could not be drawn")
                continue
            started = time.monotonic()
            try:
                rows = await client.read_rows(plan.model, images[i])
            except StructuredOutputError:
                structured_failures += 1
                warnings.append(f"page {i + 1}: this model did not return structured output (valid JSON)")
                continue
            except PageError as exc:
                warnings.append(f"page {i + 1}: reader A: {describe(exc)}")
                continue
            seconds += time.monotonic() - started
            read_pages += 1
            for r in rows:
                rows_a.append(ReaderRow(
                    name=str(r.get("name", "")), value=str(r.get("value", "")),
                    unit=str(r.get("unit", "")), reference_range=str(r.get("reference_range", "")),
                    page=i + 1,
                ))
        if not read_pages:
            if structured_failures == count:
                raise CheckError(
                    "This model did not return structured output (valid JSON) on any page, so it cannot be used "
                    "as reader A."
                )
            raise CheckError("No page could be read." + (" " + warnings[-1] if warnings else ""))

        percent = await client.gpu_percent(plan.model)  # asked while the model is still loaded
        gpu = {"size_vram_percent": percent} if percent is not None else None
        if percent is not None and percent < 100:
            warnings.append(
                f"only {percent}% of the model is in GPU memory: the rest runs on the CPU, which is much slower"
            )

        rows_b: list[ReaderRow] | None = None
        if plan.use_reader_b:
            if not glm_like(settings.reader_b_model):
                warnings.append(
                    "reader B is built for glm-ocr: other models produce text the parser cannot read"
                )
            rows_b = []
            for i in range(count):
                progress.update(stage="reader_b", page=i + 1)
                if i not in images:
                    continue
                text, error = await _read_b(client, settings, pages, i, images.pop(i))
                if error:
                    warnings.append(f"page {i + 1}: reader B: {error}")
                for r in parse_glm(text):
                    rows_b.append(ReaderRow(
                        name=r["name"], value=r["value"], unit=r["unit"],
                        reference_range=r["reference_range"], page=i + 1, code=r["code"],
                    ))
            if not rows_b:
                warnings.append("reader B produced no rows")

        progress.update(stage="verify")
        found = len(first_occurrences(rows_a))
        if not found:
            warnings.append("no rows were found on the pages that were read")
        result = {
            "model": plan.model,
            "pages": count,
            "seconds_per_page": round(seconds / read_pages, 1),
            "rows_found": found,
            "used_reader_b": plan.use_reader_b,
            "reader_b_model": settings.reader_b_model if plan.use_reader_b else None,
            "verified_by_two_readers": (
                sum(1 for c in combine(rows_a, rows_b, [], True) if c.status == VERIFIED) if rows_b is not None else None
            ),
            "expected_count": len(plan.expected),
            "matched": 0,
            "wrong_value": 0,
            "missing": 0,
            "extra_not_in_expected": None,
            "gpu": gpu,
            "warnings": warnings,
        }
        if plan.expected:
            counts = score(rows_a, plan.expected)
            result.update(matched=counts["matched"], wrong_value=counts["wrong_value"], missing=counts["missing"],
                          extra_not_in_expected=counts["extra"])
        return result
    finally:
        await asyncio.to_thread(pages.close)


# --- what is remembered: counts only ----------------------------------------------------------------------

_HISTORY_FIELDS = ("date", "pages", "seconds_per_page", "rows_found", "expected_count", "matched", "wrong_value",
                   "extra", "gpu_percent")


def history() -> dict[str, dict]:
    """{model: {date, pages, seconds_per_page, rows_found, expected_count, matched, wrong_value, extra,
    gpu_percent}} of the last checks, oldest first. A stored value that is not what it should be is ignored."""
    with Session(engine) as session:
        row = session.get(AppSetting, CHECKS_KEY)
    try:
        data = json.loads(row.value) if row else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: {k: entry.get(k) for k in _HISTORY_FIELDS}
        for name, entry in data.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


def remember(result: dict) -> None:
    """Keep the counts of this check, and nothing else (no values, no file name, no expected list)."""
    entry = {
        "date": date.today().isoformat(),
        "pages": result["pages"],
        "seconds_per_page": result["seconds_per_page"],
        "rows_found": result["rows_found"],
        "expected_count": result["expected_count"],
        "matched": result["matched"],
        "wrong_value": result["wrong_value"],
        "extra": result["extra_not_in_expected"],
        "gpu_percent": (result["gpu"] or {}).get("size_vram_percent"),
    }
    kept = history()
    kept.pop(result["model"], None)  # a new check of a model replaces its old one and counts as the newest
    kept[result["model"]] = entry
    kept = dict(list(kept.items())[-KEEP_MODELS:])
    with Session(engine) as session:
        put(session, CHECKS_KEY, json.dumps(kept))
        session.commit()


# --- the background task ----------------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()  # a reference, so a running check is not garbage collected


def start(path: str, plan: Plan, progress: dict) -> None:
    """Hand the file and the claimed place over to a background task. From here the task removes the file and
    frees the place, whatever happens."""
    progress.update(model=plan.model, stage="starting")
    task = asyncio.create_task(_run(path, plan, progress), name="vibehealth-model-check")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(path: str, plan: Plan, progress: dict) -> None:
    last: dict | None = None
    try:
        async with asyncio.timeout(HARD_TIMEOUT):
            result = await run_check(path, plan, progress)
        await asyncio.to_thread(remember, result)
        last = {"status": "done", "result": result}
    except TimeoutError:
        last = {"status": "error", "error": "The check took too long and was stopped."}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a fixed message, details in the log
        if isinstance(exc, SafeError):
            log.warning("model check failed: %s", describe(exc))
            message = describe(exc)
        else:
            log.warning("model check failed", exc_info=True)
            message = "The check failed unexpectedly. Details are in the server log."
        last = {"status": "error", "error": message}
    finally:
        uploads.remove_quietly(path)  # the sample file never outlives the check
        if last is not None:
            check_state["last"] = {**last, "model": plan.model,
                                   "finished_at": datetime.now().isoformat(timespec="seconds")}
        check_state["current"] = None
