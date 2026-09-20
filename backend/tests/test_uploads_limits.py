"""How much an upload may cost: disk, folder quota, uploads in flight, slow bodies, the periodic sweep,
and clean-up that never raises. Real concurrency (threads, an ASGI client with a slow body) is used
where the point is concurrency."""

import asyncio
import collections
import os
import sys
import threading
import time

import httpx
import pytest
from conftest import PasswordClient
from sqlmodel import Session, select
from upload_helpers import BOUNDARY, multipart, pdf

from app import sandbox, settings_store, uploads
from app.db import engine
from app.main import app
from app.models import Document

client = PasswordClient(app)
URL = "/api/documents/upload"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MB = 1024 * 1024


def upload(data: bytes, name: str = "a.pdf", **fields):
    return client.post(URL, files={"file": (name, data, "application/pdf")}, data=fields)


def count(model=Document) -> int:
    with Session(engine) as s:
        return len(s.exec(select(model)).all())


def tmp_files() -> list[str]:
    return os.listdir(uploads.tmp_root())


# --- the folder quota: uploads.max_total_mb ---------------------------------------------------------------------


def test_the_quota_setting_is_validated_and_shown():
    client.get("/api/status")
    view = client.get("/api/settings/uploads").json()
    assert view["values"]["max_total_mb"] == 10240 and view["sources"]["max_total_mb"] == "default"
    for bad in (0, 99, -1, 1_000_001, "lots", 1.5, None):
        r = client.put("/api/settings/uploads", json={"max_total_mb": bad})
        assert r.status_code == (200 if bad is None else 422), bad
    for good in (100, 4096, 1_000_000):
        r = client.put("/api/settings/uploads", json={"max_total_mb": good})
        assert r.status_code == 200 and r.json()["values"]["max_total_mb"] == good
    assert client.put("/api/settings/uploads", json={"max_total_mb": None}).json()["values"]["max_total_mb"] == 10240


def test_status_shows_what_the_uploads_folder_holds():
    client.get("/api/status")
    assert client.get("/api/status").json()["uploads"]["total_mb_used"] == 0.0
    data = pdf(1)
    assert upload(data).status_code == 201
    assert uploads.total_bytes() == len(data)
    assert client.get("/api/status").json()["uploads"]["total_mb_used"] == round(len(data) / MB, 1)
    # a temp file counts too: it is on the disk
    with open(os.path.join(uploads.tmp_root(), "half"), "wb") as f:
        f.write(b"x" * (3 * MB))
    assert uploads.total_bytes() == len(data) + 3 * MB
    assert client.get("/api/status").json()["uploads"]["total_mb_used"] == round((len(data) + 3 * MB) / MB, 1)


def test_an_upload_that_would_go_over_the_quota_is_refused_with_507(monkeypatch):
    client.get("/api/status")
    settings_store.update("uploads", {"max_total_mb": 100})
    data = pdf(1)
    monkeypatch.setattr(uploads, "total_bytes", lambda: 100 * MB)  # the folder is exactly at the limit
    r = upload(data)
    assert r.status_code == 507 and "uploads folder is full" in r.json()["detail"] and "Settings" in r.json()["detail"]
    assert count() == 0 and tmp_files() == []

    monkeypatch.setattr(uploads, "total_bytes", lambda: 100 * MB - 100)  # room for 100 bytes: not for this file
    r = upload(data)
    assert r.status_code == 507
    assert count() == 0 and tmp_files() == []

    monkeypatch.setattr(uploads, "total_bytes", lambda: 100 * MB - len(data) - 100_000)  # room enough
    assert upload(data).status_code == 201


def test_the_quota_also_stops_a_body_that_does_not_say_how_long_it_is(monkeypatch):
    """No Content-Length (chunked): the bytes are counted as they arrive against what is left."""
    client.get("/api/status")
    settings_store.update("uploads", {"max_total_mb": 100})
    monkeypatch.setattr(uploads, "total_bytes", lambda: 100 * MB - 2000)  # 2000 bytes of room
    body, headers = multipart("a.pdf", pdf(1), "application/pdf")
    assert len(pdf(1)) > 2000

    def chunks():
        for i in range(0, len(body), 1024):
            yield body[i:i + 1024]

    r = client.post(URL, content=chunks(), headers=headers)
    assert r.status_code == 507 and "no room left" in r.json()["detail"]
    assert count() == 0 and tmp_files() == []


def test_the_smallest_allowed_quota_still_takes_normal_files():
    client.get("/api/status")
    assert upload(pdf(1)).status_code == 201
    settings_store.update("uploads", {"max_total_mb": 100})  # still far above one small file
    assert upload(pdf(2)).status_code == 201


# --- free disk space: 2 x the cap ---------------------------------------------------------------------------------


def fake_free(monkeypatch, free: int):
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda path: usage(10**12, 10**12 - free, free))


def test_an_upload_is_refused_when_the_free_space_is_under_twice_the_cap(monkeypatch):
    client.get("/api/status")
    cap = uploads.cap_bytes(50)
    fake_free(monkeypatch, 2 * cap - 1)
    r = upload(pdf(1))
    assert r.status_code == 507 and "not enough free disk space" in r.json()["detail"]
    assert "100 MB" in r.json()["detail"]
    assert count() == 0 and tmp_files() == []
    fake_free(monkeypatch, 2 * cap)
    assert upload(pdf(1)).status_code == 201


def test_the_free_space_needed_grows_with_the_uploads_already_running(monkeypatch):
    cap = 50 * MB
    fake_free(monkeypatch, 3 * cap)
    uploads.check_room(cap, 10**12, others=1)  # 3 x cap needed, 3 x cap there
    with pytest.raises(uploads.UploadError) as info:
        uploads.check_room(cap, 10**12, others=2)  # 4 x cap needed
    assert info.value.status == 507


def test_the_free_space_follows_the_size_setting(monkeypatch):
    client.get("/api/status")
    settings_store.update("uploads", {"max_file_mb": 200})
    fake_free(monkeypatch, 399 * MB)
    assert upload(pdf(1)).status_code == 507
    fake_free(monkeypatch, 400 * MB)
    assert upload(pdf(1)).status_code == 201


# --- uploads in flight ---------------------------------------------------------------------------------------------


def slow_valid_child(log_dir, seconds: float):
    """A sandbox child that takes `seconds`, records when it ran, and then says the file is fine."""
    code = (
        f"import sys; sys.path.insert(0, {ROOT!r})\n"
        "import os, struct, time, json\n"
        "t0 = time.time()\n"
        f"time.sleep({seconds})\n"
        f"open(os.path.join({str(log_dir)!r}, str(os.getpid())), 'w').write(f'{{t0}} {{time.time()}}')\n"
        "m = json.dumps({'ok': True, 'pages': 1}).encode(); p = b'\\xff\\xd8\\xff\\xe0thumbnail'\n"
        "os.write(1, b'VHSB1' + struct.pack('>II', len(m), len(p)) + m + p)"
    )
    return [sys.executable, "-c", code]


def test_uploads_beyond_the_limit_get_429_with_retry_after_and_children_stay_at_two(monkeypatch, tmp_path):
    client.get("/api/status")
    logs = tmp_path / "runs"
    logs.mkdir()
    monkeypatch.setattr(sandbox, "_child_argv", lambda: slow_valid_child(logs, 2.0))
    monkeypatch.setattr(uploads, "MAX_INFLIGHT", 3)
    n = 6
    barrier = threading.Barrier(n)
    results = {}

    def go(i):
        barrier.wait()
        results[i] = upload(pdf(1, size=(595 + i, 842)), f"f{i}.pdf")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [201, 201, 201, 429, 429, 429], statuses
    for r in results.values():
        if r.status_code == 429:
            assert r.headers["retry-after"] == str(uploads.RETRY_AFTER)
            assert "Too many uploads" in r.json()["detail"]
    assert count() == 3 and uploads._inflight == 0  # every place was given back
    assert tmp_files() == []
    spans = []
    for name in os.listdir(logs):
        a, b = (float(x) for x in (logs / name).read_text().split())
        spans.append((a, b))
    assert len(spans) == 3
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans], key=lambda e: (e[0], e[1]))
    running = peak = 0
    for _t, delta in events:
        running += delta
        peak = max(peak, running)
    assert peak <= sandbox.MAX_CONCURRENT
    # and afterwards the same upload works: the places are free again
    monkeypatch.undo()
    assert upload(pdf(1, size=(700, 842))).status_code == 201


def test_a_failed_upload_gives_its_place_back():
    client.get("/api/status")
    for _ in range(uploads.MAX_INFLIGHT + 2):
        assert upload(b"%PDF-1.4 junk").status_code == 422
    assert uploads._inflight == 0
    assert upload(pdf(1)).status_code == 201


def test_begin_upload_is_thread_safe_and_never_over_admits():
    uploads._inflight = 0
    admitted, refused = [], []
    barrier = threading.Barrier(12)

    def go():
        barrier.wait()
        try:
            uploads.begin_upload()
            admitted.append(1)
        except uploads.UploadError as exc:
            refused.append(exc.status)

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(admitted) == uploads.MAX_INFLIGHT and refused == [429] * (12 - uploads.MAX_INFLIGHT)
    for _ in admitted:
        uploads.end_upload()
    assert uploads._inflight == 0


# --- slow bodies -------------------------------------------------------------------------------------------------


class FakeRequest:
    """Just what `receive` needs: headers and a body stream that we control."""

    def __init__(self, stream):
        self.headers = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
        self._stream = stream

    def stream(self):
        return self._stream


def body_of(data: bytes, filename="a.pdf") -> bytes:
    return multipart(filename, data, "application/pdf")[0]


def run_receive(stream, cap=10 * MB, quota=None):
    return asyncio.run(uploads.receive(FakeRequest(stream), cap, quota))


def test_a_fast_body_is_received_hashed_and_written_in_pieces():
    import hashlib

    data = b"%PDF-1.4\n" + os.urandom(3 * MB + 123)  # well over FLUSH_AT: several threaded writes
    body = body_of(data)

    async def stream():
        for i in range(0, len(body), 65536):
            yield body[i:i + 65536]

    got = run_receive(stream())
    try:
        assert got.size == len(data) and got.sha256 == hashlib.sha256(data).hexdigest() and got.head == data[:16]
        with open(got.path, "rb") as f:
            assert f.read() == data
        assert got.path in uploads._active_tmp  # in flight until the caller is done with it
    finally:
        uploads.remove_quietly(got.path)
    assert got.path not in uploads._active_tmp and tmp_files() == []


def test_the_file_is_written_and_hashed_off_the_event_loop():
    """The parser's callbacks only note what arrived: writing and hashing happen in a worker thread."""
    data = b"%PDF-1.4\n" + b"z" * (1 * MB)
    body = body_of(data)
    threads_seen = set()
    real_write = uploads._Sink.write

    def spy(self, chunks, ending):
        threads_seen.add(threading.current_thread() is threading.main_thread())
        return real_write(self, chunks, ending)

    async def stream():
        for i in range(0, len(body), 65536):
            yield body[i:i + 65536]

    uploads._Sink.write = spy
    try:
        got = run_receive(stream())
    finally:
        uploads._Sink.write = real_write
        uploads.remove_quietly(got.path)
    assert threads_seen == {False}  # never on the thread that runs the loop (asyncio.run: the main thread)


def test_the_loop_keeps_running_while_a_file_is_written(monkeypatch):
    body = body_of(b"%PDF-1.4\n" + b"z" * (2 * MB))
    real = uploads._Sink.write

    def slow_write(self, chunks, ending):
        time.sleep(0.15)  # a slow disk
        return real(self, chunks, ending)

    monkeypatch.setattr(uploads._Sink, "write", slow_write)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        async def stream():
            for i in range(0, len(body), 65536):
                yield body[i:i + 65536]

        task = asyncio.create_task(ticker())
        got = await uploads.receive(FakeRequest(stream()), 10 * MB)
        task.cancel()
        return got, ticks

    got, ticks = asyncio.run(scenario())
    uploads.remove_quietly(got.path)
    assert ticks >= 20  # the writes took about a second in all, the loop was answering the whole time


def test_a_body_that_stalls_is_cut_and_its_temp_file_removed(monkeypatch):
    monkeypatch.setattr(uploads, "IDLE_TIMEOUT", 0.4)
    body = body_of(b"%PDF-1.4\n" + b"x" * 200_000)

    async def stream():
        yield body[:150_000]  # the start of the file is on disk ...
        await asyncio.sleep(30)  # ... and then nothing
        yield body[150_000:]

    started = time.monotonic()
    with pytest.raises(uploads.UploadError) as info:
        run_receive(stream())
    assert info.value.status == 408 and "stalled" in info.value.message
    assert time.monotonic() - started < 5
    assert tmp_files() == [] and not uploads._active_tmp


def test_a_body_that_crawls_is_cut_by_the_minimum_rate(monkeypatch):
    monkeypatch.setattr(uploads, "RATE_GRACE", 0.3)
    monkeypatch.setattr(uploads, "MIN_RATE", 50_000)  # bytes per second
    body = body_of(b"%PDF-1.4\n" + b"x" * 100_000)

    async def stream():  # ~1 KB per 0.1 s: never idle for long, but 10 KB/s
        for i in range(0, len(body), 1000):
            yield body[i:i + 1000]
            await asyncio.sleep(0.1)

    started = time.monotonic()
    with pytest.raises(uploads.UploadError) as info:
        run_receive(stream())
    assert info.value.status == 408 and "too slow" in info.value.message
    assert time.monotonic() - started < 5
    assert tmp_files() == []


def test_the_overall_deadline_ends_a_body_that_never_finishes(monkeypatch):
    monkeypatch.setattr(uploads, "MAX_RECEIVE_SECONDS", 0.6)
    monkeypatch.setattr(uploads, "MIN_RATE", 1)  # the rate check does not fire: the deadline must

    async def stream():
        yield body_of(b"%PDF-1.4\n" + b"x" * 1000)[:200]
        while True:
            await asyncio.sleep(0.1)
            yield b"y"

    with pytest.raises(uploads.UploadError) as info:
        run_receive(stream())
    assert info.value.status == 408 and "took too long" in info.value.message
    assert tmp_files() == []


def test_a_client_that_disconnects_leaves_nothing(monkeypatch):
    from starlette.requests import ClientDisconnect

    body = body_of(b"%PDF-1.4\n" + b"x" * 300_000)

    async def stream():
        yield body[:100_000]
        raise ClientDisconnect

    with pytest.raises(uploads.UploadError) as info:
        run_receive(stream())
    assert info.value.status == 400 and tmp_files() == []


def test_a_slow_body_through_the_whole_app_is_a_408_and_leaves_nothing(monkeypatch):
    """The same, end to end: an ASGI client whose request body trickles in."""
    client.get("/api/status")
    monkeypatch.setattr(uploads, "IDLE_TIMEOUT", 0.5)
    body = body_of(pdf(1))

    async def slow():
        yield body[:300]
        await asyncio.sleep(10)
        yield body[300:]

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=client.cookies) as c:
            return await c.post(URL, content=slow(), headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"})

    started = time.monotonic()
    r = asyncio.run(go())
    assert r.status_code == 408 and "stalled" in r.json()["detail"], r.text
    assert time.monotonic() - started < 8
    assert tmp_files() == [] and count() == 0 and uploads._inflight == 0


# --- the periodic sweep ----------------------------------------------------------------------------------------------


def age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def make(path, data=b"x"):
    with open(path, "wb") as f:
        f.write(data)
    return path


def test_sweep_tmp_removes_old_files_but_not_new_ones_or_uploads_in_flight():
    folder = uploads.tmp_root()
    old, new, active = (make(os.path.join(folder, n)) for n in ("old", "new", "active"))
    age(old, 7200)
    age(active, 7200)
    uploads._active_tmp.add(active)
    try:
        assert uploads.sweep_tmp()["tmp"] == 1
        assert not os.path.exists(old) and os.path.exists(new) and os.path.exists(active)
    finally:
        uploads._active_tmp.discard(active)
    part = make(os.path.join(uploads.thumbs_root(), "a" * 64 + ".jpg.deadbeef.part"))
    fresh_part = make(os.path.join(uploads.thumbs_root(), "b" * 64 + ".jpg.cafe.part"))
    age(part, 7200)
    assert uploads.sweep_tmp()["parts"] == 1
    assert not os.path.exists(part) and os.path.exists(fresh_part)


def test_the_sweep_loop_cleans_while_the_app_runs(monkeypatch):
    monkeypatch.setattr(uploads, "SWEEP_INTERVAL", 0.05)
    old = make(os.path.join(uploads.tmp_root(), "orphan"))
    age(old, 7200)

    async def scenario():
        task = asyncio.create_task(uploads.sweep_loop())
        await asyncio.sleep(0.5)
        # a second orphan appears later: the loop is still at it
        later = make(os.path.join(uploads.tmp_root(), "orphan2"))
        age(later, 7200)
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return later

    later = asyncio.run(scenario())
    assert not os.path.exists(old) and not os.path.exists(later)


def test_the_sweep_loop_survives_a_failing_pass(monkeypatch):
    monkeypatch.setattr(uploads, "SWEEP_INTERVAL", 0.05)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        return {"tmp": 0, "parts": 0}

    monkeypatch.setattr(uploads, "sweep_tmp", flaky)

    async def scenario():
        task = asyncio.create_task(uploads.sweep_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) >= 3


def test_the_app_runs_the_sweep_every_ten_minutes_after_start(monkeypatch):
    """The lifespan starts the loop: an orphan that appears after start-up goes without a restart."""
    from fastapi.testclient import TestClient

    assert uploads.SWEEP_INTERVAL == 600
    monkeypatch.setattr(uploads, "SWEEP_INTERVAL", 0.1)
    orphan = os.path.join(uploads.tmp_root(), "orphan")
    with TestClient(app):
        make(orphan)
        age(orphan, 7200)
        deadline = time.monotonic() + 5
        while os.path.exists(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
    assert not os.path.exists(orphan)


# --- clean-up never raises -------------------------------------------------------------------------------------------


def stored(name_data=b"%PDF-1.4"):
    tmp = make(os.path.join(uploads.tmp_root(), "t" + str(time.monotonic_ns())), name_data)
    return uploads.store(tmp, "pdf")


def test_delete_files_never_raises_whatever_fails(monkeypatch):
    relative = stored()
    sha = "a" * 64

    def boom(*a, **kw):
        raise OSError("no")

    monkeypatch.setattr(os, "makedirs", boom)  # thumbs_root() cannot make its folder
    assert uploads.delete_files(relative, sha) is False
    monkeypatch.undo()
    monkeypatch.setattr(os.path, "realpath", boom)  # the path cannot even be resolved
    assert uploads.delete_files(relative, sha) is False
    assert uploads.delete_files(None, None) is True
    monkeypatch.undo()
    monkeypatch.setattr(uploads, "_thumb_file", lambda sha: (_ for _ in ()).throw(RuntimeError("odd")))
    assert uploads.delete_files(relative, sha) is False  # (the original went with the first call: not an error)


def test_delete_files_keeps_a_note_when_it_cannot_remove_and_the_next_sweep_finishes(monkeypatch):
    relative = stored()
    path = uploads.stored_file(relative)
    real_remove = os.remove

    def stuck(p, *a, **kw):
        if os.path.realpath(p) == path:
            raise PermissionError("in use")
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(os, "remove", stuck)
    assert uploads.delete_files(relative, None) is False
    assert uploads.delete_files(relative, None) is False  # asking twice does not write the line twice
    with open(os.path.join(uploads.root(), ".pending-delete"), encoding="utf-8") as f:
        assert f.read().split() == [relative]
    monkeypatch.undo()
    assert uploads.sweep_at_start()["pending"] == 1 and not os.path.exists(path)


def test_startup_sweep_survives_every_step_failing(monkeypatch):
    def boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr(os, "scandir", boom)
    monkeypatch.setattr(uploads, "_read_pending", lambda: (_ for _ in ()).throw(RuntimeError("odd")))
    assert uploads.startup_sweep(set(), set()) == {"tmp": 0, "pending": 0, "thumbs": 0}
    monkeypatch.undo()
    monkeypatch.setattr(uploads, "tmp_root", boom)
    monkeypatch.setattr(uploads, "thumbs_root", boom)
    assert uploads.startup_sweep(set(), set())["tmp"] == 0


def test_sweep_at_start_survives_a_broken_database(monkeypatch):
    import sqlmodel

    def boom(*a, **kw):
        raise RuntimeError("database gone")

    monkeypatch.setattr(sqlmodel, "Session", boom)
    stray = make(os.path.join(uploads.thumbs_root(), "c" * 64 + ".jpg"))
    assert uploads.sweep_at_start() == {"tmp": 0, "pending": 0, "thumbs": 0}
    assert os.path.exists(stray)  # and, not knowing the documents, it removed nothing


def test_a_failing_sweep_never_stops_the_app_from_starting(monkeypatch):
    from fastapi.testclient import TestClient

    def boom():
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(uploads, "sweep_at_start", boom)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True}


# --- the delete intent is written before the commit --------------------------------------------------------------------


def make_doc() -> int:
    client.get("/api/status")
    return upload(pdf(1)).json()["document"]["id"]


def pending_lines() -> list[str]:
    path = os.path.join(uploads.root(), ".pending-delete")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.read().split()


def test_the_intent_to_delete_is_on_disk_before_the_database_commit(monkeypatch):
    did = make_doc()
    with Session(engine) as s:
        relative = s.get(Document, did).stored_path
    seen = {}
    from sqlalchemy.orm import Session as OrmSession

    real_commit = OrmSession.commit

    def commit(self):
        seen.setdefault("pending_at_commit", pending_lines())
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", commit)
    r = client.delete(f"/api/documents/{did}")
    monkeypatch.undo()
    assert r.status_code == 200 and r.json() == {"deleted": True, "file_removed": True}
    assert seen["pending_at_commit"] == [relative]  # noted first
    assert pending_lines() == []  # and taken off the list once the file was gone


def test_a_process_that_dies_between_the_commit_and_the_unlink_is_finished_by_the_next_start(monkeypatch):
    did = make_doc()
    with Session(engine) as s:
        relative = s.get(Document, did).stored_path
    path = uploads.stored_file(relative)

    def dies(*a, **kw):
        raise KeyboardInterrupt("the process is killed here")

    monkeypatch.setattr(uploads, "delete_files", dies)
    with pytest.raises(KeyboardInterrupt):
        client.delete(f"/api/documents/{did}")
    monkeypatch.undo()
    assert count() == 0 and os.path.isfile(path)  # the row is gone, the file is orphaned ...
    assert pending_lines() == [relative]  # ... but the intent was written before the commit
    assert uploads.sweep_at_start()["pending"] == 1  # the next start removes it
    assert not os.path.exists(path) and pending_lines() == []


def test_a_failed_commit_leaves_the_document_and_its_file_and_a_harmless_note(monkeypatch):
    did = make_doc()
    with Session(engine) as s:
        relative = s.get(Document, did).stored_path
    path = uploads.stored_file(relative)
    from sqlalchemy.orm import Session as OrmSession

    def commit(self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(OrmSession, "commit", commit)
    with pytest.raises(RuntimeError):
        client.delete(f"/api/documents/{did}")
    monkeypatch.undo()
    assert count() == 1 and os.path.isfile(path)
    assert pending_lines() == [relative]
    result = uploads.sweep_at_start()  # a document owns the path: the note is dropped, the file kept
    assert result["pending"] == 0 and os.path.isfile(path) and pending_lines() == []
    assert client.get(f"/api/documents/{did}/preview").status_code == 200
