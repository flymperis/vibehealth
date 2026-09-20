"""The sync loop follows the Paperless settings: enabled, connected, interval, wake-up on save."""

import asyncio
import time

import pytest
from fake_paperless import BASE, TOKEN, FakePaperless
from conftest import PasswordClient
from fastapi.testclient import TestClient

from app import worker
from app.main import app

client = PasswordClient(app)


def put(**body):
    r = client.put("/api/settings/paperless", json=body)
    assert r.status_code == 200, r.text
    return r


def connect(**extra):
    put(url=BASE, token=TOKEN, **extra)


@pytest.fixture(autouse=True)
def quick_worker(monkeypatch):
    """A 'minute' lasts 20 ms; the loop would look again after 30 s if nobody woke it."""
    monkeypatch.setattr(worker, "_MINUTE", 0.02)
    monkeypatch.setattr(worker, "_MAX_WAIT", 30.0)
    monkeypatch.setattr(worker, "_manual", False)
    monkeypatch.setitem(worker.state, "last_error", None)


@pytest.fixture
def calls(monkeypatch):
    """Replaces the sync itself; records when a sync would have run."""
    seen: list[float] = []

    async def fake_run_once():
        if not worker.sync_plan().active:
            return False
        seen.append(time.monotonic())
        return True

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    return seen


def run_loop(scenario):
    async def main():
        task = asyncio.create_task(worker.loop())
        try:
            await scenario()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


async def save(**body):
    """Saved from a request thread, as the API does."""
    await asyncio.to_thread(put, **body)


async def ask_for_sync():
    await asyncio.to_thread(worker.request_sync)


async def until(condition, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


# --- run_once: skipped, not failed ---------------------------------------------------------


def test_sync_is_skipped_without_error_when_not_connected(monkeypatch):
    ran = []

    async def boom():
        ran.append(1)
        raise AssertionError("must not sync")

    monkeypatch.setattr(worker, "sync_documents", boom)
    worker.state["last_error"] = "stale"
    assert asyncio.run(worker.run_once()) is False  # no address at all
    assert worker.state["last_error"] is None and worker.state["running"] is False

    put(url=BASE)  # an address but no token
    assert asyncio.run(worker.run_once()) is False
    connect()
    put(enabled=False)  # connected but switched off
    assert asyncio.run(worker.run_once()) is False
    assert ran == []


def test_clearing_the_token_stops_the_sync_quietly(monkeypatch):
    async def ok():
        return {"seen": 0, "created": 0, "updated": 0, "new_ids": []}

    monkeypatch.setattr(worker, "sync_documents", ok)
    connect()
    assert asyncio.run(worker.run_once()) is True
    put(token="")
    assert asyncio.run(worker.run_once()) is False and worker.state["last_error"] is None


def test_a_failed_sync_is_reported_without_the_token(monkeypatch):
    fake = FakePaperless().standard().install(monkeypatch)
    connect()
    fake.status_override = 500
    assert asyncio.run(worker.run_once()) is True
    err = worker.state["last_error"]
    assert "500" in err and TOKEN not in err and worker.state["running"] is False
    fake.status_override = None
    assert asyncio.run(worker.run_once()) is True and worker.state["last_error"] is None


def test_error_text_strips_credentials_from_an_address(monkeypatch):
    from app.paperless import safe_text

    assert safe_text("failed for http://user:hunter2@host:8000/api/") == "failed for http://host:8000/api/"


# --- the endpoints tell the UI ---------------------------------------------------------------


def test_status_and_sync_now_explain_why_nothing_happens():
    s = client.get("/api/status").json()["paperless"]
    assert s == {"enabled": True, "configured": False, "sync_active": False,
                 "skipped": "not_configured", "sync_interval_minutes": 240}
    assert client.post("/api/sync").json() == {"ok": True, "queued": False, "reason": "not_configured"}

    connect(enabled=False, sync_interval_minutes=0)
    s = client.get("/api/status").json()["paperless"]
    assert s["skipped"] == "disabled" and s["sync_active"] is False and s["sync_interval_minutes"] == 0
    assert client.post("/api/sync").json()["reason"] == "disabled"

    put(enabled=True)
    s = client.get("/api/status").json()["paperless"]
    assert s["sync_active"] is True and s["skipped"] is None
    assert client.post("/api/sync").json() == {"ok": True, "queued": True, "reason": None}


def test_env_interval_below_the_minimum_is_clamped(monkeypatch, reload_config):
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "2")
    reload_config()
    assert worker.sync_plan().minutes == 5
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "0")
    reload_config()
    assert worker.sync_plan().minutes == 0


# --- the loop --------------------------------------------------------------------------------


def test_loop_runs_on_the_saved_interval_and_stops_at_zero(calls):
    connect(sync_interval_minutes=5)  # 5 "minutes" = 100 ms here

    async def scenario():
        assert await until(lambda: len(calls) >= 3)
        await save(sync_interval_minutes=0)
        await asyncio.sleep(0.05)
        settled = len(calls)
        await asyncio.sleep(0.5)  # five periods with nothing due
        assert len(calls) == settled
        # manual only
        await ask_for_sync()
        assert await until(lambda: len(calls) == settled + 1)
        await asyncio.sleep(0.3)
        assert len(calls) == settled + 1

    run_loop(scenario)


def test_the_interval_is_read_every_pass_not_once(calls):
    connect(sync_interval_minutes=5)

    async def scenario():
        assert await until(lambda: len(calls) >= 2)
        await save(sync_interval_minutes=10000)  # ~200 s: nothing more is due
        await asyncio.sleep(0.05)
        settled = len(calls)
        await asyncio.sleep(0.4)
        assert len(calls) == settled
        await save(sync_interval_minutes=5)  # short again: due at once
        assert await until(lambda: len(calls) > settled, 1.0)

    run_loop(scenario)


def test_saving_wakes_the_loop_at_once(calls):
    connect(sync_interval_minutes=10000)  # one sync at start, then a very long wait

    async def scenario():
        assert await until(lambda: len(calls) == 1)
        await asyncio.sleep(0.15)
        assert len(calls) == 1  # asleep (and the loop would not look again for 30 s)
        started = time.monotonic()
        await save(sync_interval_minutes=5)  # 100 ms, long since passed
        assert await until(lambda: len(calls) == 2, 1.0)
        assert time.monotonic() - started < 1.0

    run_loop(scenario)


def test_disabled_does_nothing_until_enabled(calls):
    connect(enabled=False)

    async def scenario():
        await asyncio.sleep(0.3)
        assert calls == [] and worker.state["last_error"] is None
        await ask_for_sync()
        await asyncio.sleep(0.2)
        assert calls == []  # even a manual request: nothing is queued while off
        await save(enabled=True)
        assert await until(lambda: len(calls) >= 1, 1.0)

    run_loop(scenario)


def test_not_connected_does_nothing_until_connected(calls):
    async def scenario():
        await asyncio.sleep(0.3)
        assert calls == [] and worker.state["next_sync"] is None
        await save(url=BASE, token=TOKEN)
        assert await until(lambda: len(calls) >= 1, 1.0)
        assert worker.state["next_sync"] is not None

    run_loop(scenario)


def test_manual_only_mode_does_not_sync_at_start_but_does_when_asked(calls):
    connect(sync_interval_minutes=0)

    async def scenario():
        await asyncio.sleep(0.3)
        assert calls == [] and worker.state["next_sync"] is None
        await ask_for_sync()
        assert await until(lambda: len(calls) == 1, 1.0)

    run_loop(scenario)


def test_a_request_made_before_the_loop_starts_is_not_lost(calls):
    connect(sync_interval_minutes=0)
    worker.request_sync()  # no loop yet

    async def scenario():
        assert await until(lambda: len(calls) == 1, 1.0)

    run_loop(scenario)


def test_the_loop_survives_a_failing_iteration(monkeypatch):
    seen = []

    async def flaky():
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(worker, "run_once", flaky)
    connect(sync_interval_minutes=5)
    monkeypatch.setattr(worker, "_MAX_WAIT", 0.05)

    async def scenario():
        await ask_for_sync()
        assert await until(lambda: len(seen) >= 2, 2.0)

    run_loop(scenario)
