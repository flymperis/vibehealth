"""Read lab values out of one document.

load the file (Paperless download, or the upload from disk; memory only) -> render pages -> reader A (qwen3.5, JSON rows)
-> reader B (glm-ocr text -> glm_parser, dpi fallback when cut short)
-> Paperless OCR text as the third source (none for an upload) -> verify.combine -> save.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from sqlalchemy import delete
from sqlmodel import Session, col, func, select

from . import reading_settings, sources
from .catalog import num, to_canonical_unit
from .db import engine
from .glm_parser import parse as parse_glm
from .glm_parser import truncated
from .models import Document, ExtractedValue, ExtractionRun, RunStatus, ValueStatus
from .ollama import Ollama, OllamaError, PageError, SafeError, describe
from .paperless import Paperless
from .render import Pages
from .sandbox import Refused, SandboxError
from .verify import Candidate, ReaderRow, combine, text_lines

log = logging.getLogger("vibehealth")


class ReadingError(SafeError):
    pass


def _set(progress: dict, **values) -> None:
    progress.update(values)


async def _check_models(client: Ollama, wanted: list[str]) -> None:
    installed = await client.models()
    names = set(installed) | {m.removesuffix(":latest") for m in installed}
    for model in wanted:
        if model not in names:
            raise OllamaError(f"model {model} is not installed in Ollama")


async def read_document(document_id: int, progress: dict) -> dict:
    """Run the whole pipeline for one document. Returns the run summary."""
    settings = reading_settings.load()
    if not settings.enabled:
        raise ReadingError("reading is turned off in Settings")

    with Session(engine) as session:
        doc = session.get(Document, document_id)
        if doc is None:
            raise ReadingError(f"document {document_id} not found")
        ref, title = sources.DocRef.of(doc), doc.title
        run = ExtractionRun(document_id=document_id, settings=settings.model_dump_json())
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    started = time.monotonic()
    page_errors: list[dict] = []
    pages: Pages | None = None
    _set(progress, document_id=document_id, title=title, stage="download", page=0, pages=0)
    try:
        paperless = Paperless()
        client = Ollama(settings.ollama_url, settings.timeout_seconds, settings.num_ctx, settings.keep_alive)
        models = [settings.reader_a_model] + ([settings.reader_b_model] if settings.reader_b_enabled else [])
        await _check_models(client, models)

        # Paperless: downloaded and drawn in this process, as before. Upload: drawn by the sandbox.
        pages, content = await sources.load_pages(ref, paperless=paperless, use_text=settings.use_paperless_text)
        total = len(pages)
        _set(progress, pages=total)

        # Reader A, page by page (the GPU holds one model at a time).
        rows_a: list[ReaderRow] = []
        images: dict[int, bytes] = {}
        undrawn: set[int] = set()  # pages the sandbox could not draw (an upload only): a page error, not the end
        for i in range(total):
            _set(progress, stage="reader_a", page=i + 1)
            try:
                images[i] = await asyncio.to_thread(pages.png, i, settings.dpi)
            except (SandboxError, Refused) as exc:
                undrawn.add(i)
                page_errors.append({"page": i + 1, "reader": "A", "error": "the page could not be drawn: " + describe(exc)})
                continue
            try:
                for r in await client.read_rows(settings.reader_a_model, images[i]):
                    rows_a.append(ReaderRow(
                        name=str(r.get("name", "")), value=str(r.get("value", "")),
                        unit=str(r.get("unit", "")), reference_range=str(r.get("reference_range", "")),
                        page=i + 1,
                    ))
            except PageError as exc:
                page_errors.append({"page": i + 1, "reader": "A", "error": describe(exc)})

        # Reader B, with the dpi fallback when glm-ocr stops early.
        rows_b: list[ReaderRow] | None = None
        if settings.reader_b_enabled:
            rows_b = []
            for i in range(total):
                _set(progress, stage="reader_b", page=i + 1)
                if i in undrawn:
                    page_errors.append({"page": i + 1, "reader": "B", "error": "the page could not be drawn"})
                    continue
                text, error = await _read_b(client, settings, pages, i, images.pop(i))
                if error:
                    page_errors.append({"page": i + 1, "reader": "B", "error": error})
                for r in parse_glm(text):
                    rows_b.append(ReaderRow(
                        name=r["name"], value=r["value"], unit=r["unit"],
                        reference_range=r["reference_range"], page=i + 1, code=r["code"],
                    ))

        _set(progress, stage="verify", page=total)
        failed_a = [e for e in page_errors if e["reader"] == "A"]
        if total and len(failed_a) == total and not rows_b:
            # Keep whatever an earlier reading left rather than replace it with nothing.
            raise ReadingError("no page could be read: " + failed_a[0]["error"])
        candidates = combine(rows_a, rows_b, text_lines(content), settings.reader_b_enabled)
        counts = save_candidates(document_id, run_id, candidates)
        return _finish(run_id, RunStatus.DONE, started, total, page_errors, "", counts)
    except Exception as exc:
        message = describe(exc)
        if isinstance(exc, SafeError):
            log.warning("reading document %s failed: %s", document_id, message)
        else:  # the class name goes to the UI; the details stay in the log
            log.warning("reading document %s failed", document_id, exc_info=True)
        _finish(run_id, RunStatus.ERROR, started, len(pages) if pages else 0, page_errors, message, {})
        raise
    finally:
        if pages is not None:
            await asyncio.to_thread(pages.close)  # (Paperless pages: closing takes the pdfium lock, off the loop)


async def _read_b(client: Ollama, settings, pages: Pages, index: int, first_png: bytes) -> tuple[str, str]:
    """(text, error). A truncated answer is retried at each fallback dpi; if all
    are cut short the first text is kept (its rows before the stop are fine)."""
    partial, last_error = "", ""
    for n, dpi in enumerate([settings.dpi, *settings.fallback_dpis]):
        try:
            png = first_png if n == 0 else await asyncio.to_thread(pages.png, index, dpi)
        except (SandboxError, Refused) as exc:
            last_error = f"{describe(exc)} at {dpi} dpi"
            continue
        try:
            text = await client.read_text(settings.reader_b_model, png)
        except PageError as exc:
            last_error = f"{describe(exc)} at {dpi} dpi"
            continue
        if not truncated(text):
            return text, ""
        partial = partial or text
        last_error = f"text cut short at {dpi} dpi"
    return partial, last_error + (" (kept the partial text)" if partial else "")


def _finish(run_id, status, started, pages, page_errors, error, counts) -> dict:
    with Session(engine) as session:
        run = session.get(ExtractionRun, run_id)
        run.status = status
        run.finished_at = datetime.now()
        run.duration_s = round(time.monotonic() - started, 1)
        run.pages = pages
        run.page_errors = json.dumps(page_errors, ensure_ascii=False)
        run.error = error
        run.verified = counts.get("verified", 0)
        run.needs_review = counts.get("needs_review", 0)
        run.kept_approved = counts.get("kept_approved", 0)
        session.add(run)
        session.commit()
        return run_summary(run)


def run_summary(run: ExtractionRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at.isoformat(timespec="seconds"),
        "duration_s": run.duration_s,
        "pages": run.pages,
        "page_errors": json.loads(run.page_errors or "[]"),
        "error": run.error,
        "verified": run.verified,
        "needs_review": run.needs_review,
        "kept_approved": run.kept_approved,
    }


# --- storing -----------------------------------------------------------------


def clear_unapproved(session: Session, document_id: int) -> int:
    result = session.execute(
        delete(ExtractedValue).where(
            ExtractedValue.document_id == document_id,
            ExtractedValue.status != ValueStatus.APPROVED,
        )
    )
    return result.rowcount or 0


def mark_cleared(session: Session, document_id: int) -> None:
    """After Clear left nothing, show the document as not read again."""
    remaining = session.exec(
        select(func.count()).select_from(ExtractedValue).where(ExtractedValue.document_id == document_id)
    ).one()
    run = session.exec(
        select(ExtractionRun)
        .where(ExtractionRun.document_id == document_id)
        .order_by(col(ExtractionRun.id).desc())
    ).first()
    if not remaining and run is not None and run.status == RunStatus.DONE:
        run.status = RunStatus.CLEARED
        session.add(run)


def approved_codes(session: Session, document_id: int) -> set[str]:
    return set(session.exec(
        select(ExtractedValue.test_code).where(
            ExtractedValue.document_id == document_id,
            ExtractedValue.status == ValueStatus.APPROVED,
            col(ExtractedValue.test_code).is_not(None),
        )
    ).all())


def save_candidates(document_id: int, run_id: int | None, candidates: list[Candidate]) -> dict:
    """Replace the document's unapproved values. Approved values stay, and a
    test that already has an approved value gets no new row."""
    counts = {"verified": 0, "needs_review": 0, "kept_approved": 0}
    with Session(engine) as session:
        keep = approved_codes(session, document_id)
        clear_unapproved(session, document_id)
        for c in candidates:
            if c.test_code and c.test_code in keep:
                counts["kept_approved"] += 1
                continue
            value_num, unit = to_canonical_unit(c.test_code, num(c.value), c.unit)
            session.add(ExtractedValue(
                document_id=document_id, run_id=run_id, test_code=c.test_code,
                raw_name=c.raw_name[:200], value_text=c.value[:100], value_num=value_num,
                unit=unit[:40], ref_range=c.ref_range[:80], flag=c.flag,
                status=ValueStatus(c.status), reason=c.reason, page=c.page,
                reader_a=c.reader_a, reader_b=c.reader_b,
            ))
            counts[c.status] += 1
        session.commit()
    return counts


def mark_interrupted() -> None:
    """Runs left 'running' by a restart will never finish."""
    with Session(engine) as session:
        for run in session.exec(select(ExtractionRun).where(ExtractionRun.status == RunStatus.RUNNING)):
            run.status = RunStatus.INTERRUPTED
            run.error = "interrupted by a restart"
            run.finished_at = datetime.now()
            session.add(run)
        session.commit()


# --- status for the UI ---------------------------------------------------------


def reading_states(session: Session, document_ids: list[int], queue: list[int], current: dict | None) -> dict[int, dict]:
    """Per document: not_read / queued / reading / done / error, with counts."""
    if not document_ids:
        return {}
    latest_ids = session.exec(
        select(func.max(ExtractionRun.id))
        .where(col(ExtractionRun.document_id).in_(document_ids))
        .group_by(ExtractionRun.document_id)
    ).all()
    runs = {
        r.document_id: r
        for r in session.exec(select(ExtractionRun).where(col(ExtractionRun.id).in_(latest_ids)))
    }
    counts: dict[int, dict[str, int]] = {}
    for doc_id, status, n in session.exec(
        select(ExtractedValue.document_id, ExtractedValue.status, func.count())
        .where(col(ExtractedValue.document_id).in_(document_ids))
        .group_by(ExtractedValue.document_id, ExtractedValue.status)
    ):
        counts.setdefault(doc_id, {})[status] = n

    out: dict[int, dict] = {}
    for doc_id in document_ids:
        c = counts.get(doc_id, {})
        state = {
            "state": "not_read",
            "verified": c.get(ValueStatus.VERIFIED, 0),
            "needs_review": c.get(ValueStatus.NEEDS_REVIEW, 0),
            "approved": c.get(ValueStatus.APPROVED, 0),
            "error": "",
            "page": 0,
            "pages": 0,
            "stage": "",
        }
        run = runs.get(doc_id)
        if current and current.get("document_id") == doc_id:
            state.update(state="reading", page=current.get("page", 0), pages=current.get("pages", 0),
                         stage=current.get("stage", ""))
        elif doc_id in queue:
            state["state"] = "queued"
        elif run is not None:
            if run.status == RunStatus.DONE:
                state["state"] = "done"
            elif run.status == RunStatus.CLEARED:
                state["state"] = "done" if any(c.values()) else "not_read"
            else:
                state["state"] = "error"
                state["error"] = run.error or run.status
        elif any(c.values()):
            state["state"] = "done"
        out[doc_id] = state
    return out
