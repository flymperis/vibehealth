"""Background loops: Paperless sync, and the reading queue (one document at a time)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from . import reading_settings, settings_store
from .ollama import describe
from .paperless import classify
from .reading import mark_interrupted, read_document
from .services import sync_documents

log = logging.getLogger("vibehealth")

state: dict = {
    "running": False,
    "last_sync": None,
    "last_sync_result": None,
    "next_sync": None,  # when the next automatic sync is due, if there is one
    "last_error": None,
    "reading": {
        "current": None,  # {document_id, title, stage, page, pages, started_at}
        "queue": [],  # document ids waiting
        "last_run": None,  # {document_id, title, status, duration_s, verified, needs_review, error}
        "last_error": None,
    },
}

# The model check (model_check.py) runs one at a time and never through the reading queue. It is kept
# apart from `state` because /api/status shows `state` as it is. `current`: {model, stage, page, pages,
# started_at} while one runs; `last`: {status, finished_at, result | error} of the last one (memory only).
check_state: dict = {"current": None, "last": None}

_read_wake = asyncio.Event()

# The sync loop is asked to look again (a sync was requested, or settings changed)
# from request threads as well as from its own loop, so it is signalled through
# its event loop. `_manual` says a sync was asked for; it survives a lost wake-up.
_wake: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None
_manual = False
_MINUTE = 60.0  # seconds in a "minute" (tests shorten it)
_MAX_WAIT = 300.0  # look at the settings at least this often


def _signal() -> None:
    wake, owner = _wake, _wake_loop
    if wake is None or owner is None or owner.is_closed():
        return  # the loop is not running: it reads `_manual` and the settings when it starts
    try:
        here = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    if here is owner:
        wake.set()
    else:
        owner.call_soon_threadsafe(wake.set)


def request_sync() -> None:
    """Called by the 'Sync now' button."""
    global _manual
    _manual = True
    _signal()


def settings_changed() -> None:
    """Paperless settings were saved: the sync loop re-reads them at once."""
    _signal()


@dataclass(frozen=True)
class SyncPlan:
    enabled: bool
    configured: bool  # an address and a token
    minutes: int  # 0: only when asked

    @property
    def active(self) -> bool:
        return self.enabled and self.configured

    @property
    def skipped(self) -> str | None:
        return "disabled" if not self.enabled else None if self.configured else "not_configured"


def sync_plan() -> SyncPlan:
    """The Paperless sync settings as they are right now (never a cached copy)."""
    cfg = settings_store.resolve("paperless", use_cache=False).values
    minutes = max(int(cfg["sync_interval_minutes"] or 0), 0)
    if 0 < minutes < settings_store.MIN_SYNC_MINUTES:
        minutes = settings_store.MIN_SYNC_MINUTES  # values from the environment are clamped, not refused
    return SyncPlan(bool(cfg["enabled"]), bool(cfg["url"] and cfg["token"]), minutes)


# Documents being deleted. A document is either being read or being deleted, never both: both
# claims are made under this lock, from request threads and from the reading loop.
_busy_lock = threading.Lock()
_deleting: set[int] = set()
# Queued documents that are to be read for lab values whatever their kind (a text report with a lab page in it).
_force_lab: set[int] = set()


def request_read(document_ids: list[int], force_lab: bool = False) -> list[int]:
    """Queue documents for reading; returns the ones actually added. `force_lab`: read them with the lab
    readers even if their kind says text report."""
    reading = state["reading"]
    with _busy_lock:
        current = (reading["current"] or {}).get("document_id")
        added = [d for d in document_ids if d not in reading["queue"] and d != current and d not in _deleting]
        reading["queue"].extend(added)
        if force_lab:
            _force_lab.update(added)
    if added:
        _read_wake.set()
    return added


def begin_delete(document_id: int) -> bool:
    """Claim a document for deletion. False when it is queued or being read (the caller answers 409):
    the reading would write values for a document that no longer exists."""
    with _busy_lock:
        if is_busy(document_id):
            return False
        _deleting.add(document_id)
        return True


def end_delete(document_id: int) -> None:
    with _busy_lock:
        _deleting.discard(document_id)


def is_busy(document_id: int) -> bool:
    reading = state["reading"]
    return document_id in reading["queue"] or (reading["current"] or {}).get("document_id") == document_id


def _sync_error(exc: BaseException) -> str:
    """What the UI shows for a failed sync: no token, no credentials in an address."""
    if isinstance(exc, httpx.HTTPError | httpx.InvalidURL):
        return classify(exc, settings_store.value("paperless", "url"))[1]
    return describe(exc)


async def run_once() -> bool:
    """One sync, unless Paperless is off or not connected (then quietly nothing).
    Returns whether a sync was attempted."""
    plan = sync_plan()
    if not plan.active:
        state["last_error"] = None
        return False
    state["running"] = True
    try:
        result = await sync_documents()
        new_ids = result.pop("new_ids", [])
        state["last_sync_result"] = result
        state["last_sync"] = datetime.now().isoformat(timespec="seconds")
        state["last_error"] = None
        settings = reading_settings.load()
        if new_ids and settings.enabled and settings.auto_read_after_sync:
            request_read(new_ids)
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI
        state["last_error"] = _sync_error(exc)
        if isinstance(exc, httpx.HTTPError | httpx.InvalidURL):
            log.warning("sync failed: %s", state["last_error"])
        else:
            log.exception("sync failed")
    finally:
        state["running"] = False
    return True


async def loop() -> None:
    """Runs for the lifetime of the app. Every pass it reads the Paperless settings
    again, so a change (enabled, address, interval) applies at once.

    A sync runs when someone asked for one, and, while sync is active and the
    interval is above 0, when that interval has passed since the last one (or none
    has run yet: at start, or as soon as Paperless is connected). Interval 0 means
    only when asked.
    """
    global _wake, _wake_loop, _manual
    _wake, _wake_loop = asyncio.Event(), asyncio.get_running_loop()
    last_run: float | None = None
    try:
        while True:
            timeout = _MAX_WAIT
            try:
                plan = sync_plan()
                manual, _manual = _manual, False
                period = plan.minutes * _MINUTE
                due = plan.active and plan.minutes > 0 and (
                    last_run is None or time.monotonic() - last_run >= period
                )
                if manual or due:
                    if await run_once():
                        last_run = time.monotonic()
                plan = sync_plan()  # it may have changed while the sync ran
                period = plan.minutes * _MINUTE
                if plan.active and plan.minutes > 0:
                    left = 0.0 if last_run is None else last_run + period - time.monotonic()
                    timeout = min(max(left, 0.0), _MAX_WAIT)
                    state["next_sync"] = (datetime.now() + timedelta(seconds=max(left, 0.0))).isoformat(
                        timespec="seconds"
                    )
                else:
                    state["next_sync"] = None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker iteration failed")
                timeout = min(30.0, _MAX_WAIT)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=max(timeout, 0.01))
            except TimeoutError:
                pass
            finally:
                _wake.clear()
    finally:
        _wake = _wake_loop = None


async def read_one(document_id: int) -> None:
    reading = state["reading"]
    progress = {"document_id": document_id, "title": "", "stage": "queued", "page": 0, "pages": 0,
                "started_at": datetime.now().isoformat(timespec="seconds")}
    with _busy_lock:
        force_lab = document_id in _force_lab
        _force_lab.discard(document_id)
        if document_id in _deleting:  # deleted while it waited in the queue
            return
        reading["current"] = progress
    try:
        summary = await read_document(document_id, progress, force_lab)
        reading["last_run"] = {"document_id": document_id, "title": progress["title"], **summary}
        reading["last_error"] = None
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI
        message = describe(exc)
        reading["last_error"] = message
        reading["last_run"] = {"document_id": document_id, "title": progress["title"],
                               "status": "error", "error": message}
    finally:
        reading["current"] = None


async def reading_loop() -> None:
    """Takes documents off the queue one by one: the GPU runs one model at a time."""
    mark_interrupted()
    reading = state["reading"]
    while True:
        if not reading["queue"]:
            _read_wake.clear()
            await _read_wake.wait()
            continue
        if check_state["current"] is not None:  # a model check has the GPU: the queue waits for it
            await asyncio.sleep(1)
            continue
        document_id = reading["queue"].pop(0)
        try:
            await read_one(document_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("reading loop iteration failed")
