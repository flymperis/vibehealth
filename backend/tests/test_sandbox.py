"""The sandbox child: hangs, crashes, memory blow-ups and hostile output are contained; nothing is
parsed in the server process; the server keeps serving.

The misbehaving children are small `python -c` programs put in place of the real one through
`sandbox._child_argv`; the well-behaved commands are the real ones.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time

import pytest
from conftest import PasswordClient
from sqlmodel import Session, select
from upload_helpers import pdf, photo

from app import reading, render, sandbox, sandbox_child, uploads
from app.db import engine
from app.main import app
from app.models import Document

client = PasswordClient(app)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name == "posix"


def child(code: str):
    """An argv for a child that runs `code` (with the backend on sys.path) instead of the sandbox."""
    return [sys.executable, "-c", f"import sys; sys.path.insert(0, {ROOT!r}); {code}"]


def with_command(name: str, body: str) -> list[str]:
    """A child that speaks the real protocol (real limits, real frames) and has one extra command."""
    return child(
        "from app import sandbox_child as c\n"
        f"{body}\n"
        f"c.COMMANDS[{name!r}] = handler\n"
        "c.main()"
    )


def alive(pid: int) -> bool:
    if POSIX:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


@pytest.fixture
def fast(monkeypatch):
    """Short budgets so that a hang costs a second or two, not 45 s."""
    monkeypatch.setitem(sandbox.TIMEOUTS, "check_upload", 2)
    monkeypatch.setitem(sandbox.TIMEOUTS, "thumbnail", 2)
    monkeypatch.setitem(sandbox.TIMEOUTS, "render_page", 2)
    monkeypatch.setitem(sandbox.TIMEOUTS, "page_count", 2)


def use_child(monkeypatch, argv):
    monkeypatch.setattr(sandbox, "_child_argv", lambda: argv)


def slots_free() -> bool:
    return sandbox._slots._value == sandbox.MAX_CONCURRENT


# --- the real commands, through the real child ---------------------------------------------------------


def test_the_real_child_checks_and_draws_a_thumbnail(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(pdf(3))
    result = sandbox.run("check_upload", {"path": str(path), "kind": "pdf"})
    assert result.meta["ok"] is True and result.meta["pages"] == 3
    assert result.payload.startswith(b"\xff\xd8\xff") and len(result.payload) < 50_000
    assert sandbox.run("page_count", {"path": str(path)}).meta["pages"] == 3


def test_a_refusal_comes_back_as_refused_with_our_message(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a pdf at all")
    with pytest.raises(sandbox.Refused, match="damaged"):
        sandbox.run("check_upload", {"path": str(path), "kind": "pdf"})
    assert slots_free()


def test_a_path_that_does_not_exist_is_a_refusal_not_a_crash(tmp_path):
    with pytest.raises(sandbox.Refused):
        sandbox.run("check_upload", {"path": str(tmp_path / "nope.png"), "kind": "png"})
    with pytest.raises(sandbox.Refused):
        sandbox.run("render_page", {"path": str(tmp_path / "nope.pdf"), "kind": "pdf", "index": 0, "dpi": 150})


@pytest.mark.parametrize("args", [
    {"path": 5, "kind": "pdf"}, {"path": "", "kind": "pdf"}, {"path": "x\0y", "kind": "pdf"},
    {"path": "x", "kind": "exe"}, {"kind": "pdf"},
])
def test_bad_arguments_are_refused(args):
    with pytest.raises(sandbox.Refused):
        sandbox.run("check_upload", args)


def test_an_unknown_command_is_refused():
    with pytest.raises(sandbox.Refused):
        sandbox.run("format_disk", {})


# --- a hang is killed ---------------------------------------------------------------------------------------


def test_a_child_that_hangs_is_killed_at_the_timeout(monkeypatch, tmp_path, fast):
    pid_file = tmp_path / "pid"
    use_child(monkeypatch, child(f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(600)"))
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxError) as info:
        sandbox.run("check_upload", {"path": "x", "kind": "pdf"})
    assert info.value.kind == "timeout" and "too long" in str(info.value)
    assert time.monotonic() - started < 15  # 2 s budget, not 600
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid), "the child must not outlive the call"
    assert slots_free()


def test_a_busy_loop_is_killed_too(monkeypatch, fast):
    use_child(monkeypatch, child("exec('while True: pass')"))
    with pytest.raises(sandbox.SandboxError) as info:
        sandbox.run("thumbnail", {"path": "x", "kind": "pdf"})
    assert info.value.kind == "timeout" and slots_free()


def test_the_server_keeps_serving_while_a_hostile_pdf_hangs_and_afterwards(monkeypatch, tmp_path, fast):
    client.get("/api/status")
    real = sandbox._child_argv
    use_child(monkeypatch, child("import time; time.sleep(600)"))
    r = client.post("/api/documents/upload", files={"file": ("evil.pdf", pdf(1), "application/pdf")})
    assert r.status_code == 422 and "took too long" in r.json()["detail"], r.text
    assert client.get("/api/health").status_code == 200
    assert slots_free() and sandbox.GATE._free == sandbox.MAX_CONCURRENT
    assert os.listdir(uploads.tmp_root()) == []  # nothing kept, nothing left over
    with Session(engine) as s:
        assert s.exec(select(Document)).all() == []
    # ... and the very next, honest upload works
    monkeypatch.setattr(sandbox, "_child_argv", real)
    assert client.post("/api/documents/upload", files={"file": ("ok.pdf", pdf(1), "application/pdf")}).status_code == 201


def test_a_hang_does_not_freeze_the_event_loop(monkeypatch, fast):
    """While a child hangs, other requests on the same loop are answered."""
    use_child(monkeypatch, child("import time; time.sleep(600)"))

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        with pytest.raises(sandbox.SandboxError):
            await sandbox.arun("check_upload", {"path": "x", "kind": "pdf"})
        task.cancel()
        return ticks

    assert asyncio.run(scenario()) >= 10  # ~2 s of ticks at 50 ms: the loop ran the whole time


# --- a crash, and hostile output ------------------------------------------------------------------------


@pytest.mark.parametrize("code", [
    "import os; os._exit(3)",
    "import os; os.abort()",
    "raise SystemExit(0)",  # exits cleanly but says nothing
    "import os; os.write(1, b'hello, not a frame')",
    "import os; os.write(1, b'VHSB1' + b'\\xff' * 8 + b'{}')",  # sizes that do not add up
    "import os, struct; m = b'[1]'; os.write(1, b'VHSB1' + struct.pack('>II', len(m), 0) + m)",  # meta not an object
    "import os, struct; m = b'{\"ok\": 1}'; os.write(1, b'VHSB1' + struct.pack('>II', len(m), 0) + m)",  # ok not a bool
    "import os, struct; m = b'\\xff\\xfe'; os.write(1, b'VHSB1' + struct.pack('>II', len(m), 0) + m)",
    "import sys; sys.stdin.close(); import os; os.write(1, b'')",
])
def test_a_crash_or_garbage_is_a_clean_error(monkeypatch, code):
    use_child(monkeypatch, child(code))
    with pytest.raises(sandbox.SandboxError) as info:
        sandbox.run("check_upload", {"path": "x", "kind": "pdf"})
    assert info.value.kind == "crash" and "could not be read" in str(info.value)
    assert slots_free()


def test_a_child_that_cannot_start_is_a_clean_error(monkeypatch):
    use_child(monkeypatch, ["/nonexistent/python-for-the-sandbox"])
    with pytest.raises(sandbox.SandboxError) as info:
        sandbox.run("check_upload", {"path": "x", "kind": "pdf"})
    assert info.value.kind == "crash" and slots_free()


def test_output_over_the_cap_kills_the_child(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    code = (
        f"import os, sys; open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "import struct; m = b'{\"ok\": true}'\n"
        "out = sys.stdout.buffer\n"
        "out.write(b'VHSB1' + struct.pack('>II', len(m), 500_000_000) + m)\n"
        "while True: out.write(b'x' * 65536); out.flush()"
    )
    use_child(monkeypatch, child(code))
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxError) as info:
        sandbox.run("thumbnail", {"path": "x", "kind": "pdf"}, max_payload=100_000)
    assert info.value.kind == "output" and time.monotonic() - started < 20
    time.sleep(0.3)
    assert not alive(int(pid_file.read_text()))


def test_a_message_from_the_child_is_cleaned(monkeypatch):
    code = (
        "import os, struct, json; m = json.dumps({'ok': False, 'message': 'bad\\x00\\x1b[31m thing ' + 'x' * 900}).encode()\n"
        "os.write(1, b'VHSB1' + struct.pack('>II', len(m), 0) + m)"
    )
    use_child(monkeypatch, child(code))
    with pytest.raises(sandbox.Refused) as info:
        sandbox.run("check_upload", {"path": "x", "kind": "pdf"})
    text = str(info.value)
    assert len(text) <= 300 and "\x00" not in text and "\x1b" not in text


def test_a_library_that_prints_cannot_corrupt_the_answer(monkeypatch):
    use_child(monkeypatch, with_command("noisy", "def handler(a):\n    import os; os.write(1, b'garbage on stdout'); print('more'); return {'ok': True}, b'data'"))
    result = sandbox.run("noisy", {})
    assert result.payload == b"data"


# --- what the child is given -------------------------------------------------------------------------------


def test_the_child_environment_carries_none_of_our_secrets(monkeypatch):
    for name in ("SECRET_KEY", "APP_PASSWORD_HASH", "PAPERLESS_TOKEN", "OLLAMA_URL", "DATA_DIR"):
        monkeypatch.setenv(name, "must-not-reach-the-child")
    body = "def handler(a):\n    import os, json; return {'ok': True}, json.dumps(sorted(os.environ)).encode()"
    use_child(monkeypatch, with_command("env", body))
    import json

    names = json.loads(sandbox.run("env", {}).payload)
    assert names and not {"SECRET_KEY", "APP_PASSWORD_HASH", "PAPERLESS_TOKEN", "OLLAMA_URL", "DATA_DIR"} & set(names)


@pytest.mark.skipif(not POSIX, reason="resource limits exist on POSIX only")
def test_posix_limits_are_applied_in_the_child(monkeypatch):
    monkeypatch.setattr(sandbox, "MEMORY_MB", 512)
    body = (
        "def handler(a):\n"
        "    import resource, json\n"
        "    got = {n: resource.getrlimit(getattr(resource, n)) for n in ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_CORE', 'RLIMIT_NOFILE')}\n"
        "    return {'ok': True}, json.dumps(got).encode()"
    )
    use_child(monkeypatch, with_command("limits", body))
    import json

    result = sandbox.run("limits", {}, timeout=10)
    got = json.loads(result.payload)
    assert got["RLIMIT_AS"][0] == 512 * 1024 * 1024
    assert got["RLIMIT_CORE"] == [0, 0]
    assert got["RLIMIT_NOFILE"][0] <= sandbox.OPEN_FILES
    assert got["RLIMIT_CPU"][0] == 10 + sandbox.CPU_MARGIN
    assert {"as", "cpu", "core", "nofile"} <= set(result.limits)


@pytest.mark.skipif(not POSIX, reason="resource limits exist on POSIX only")
def test_a_child_that_allocates_too_much_is_contained(monkeypatch):
    monkeypatch.setattr(sandbox, "MEMORY_MB", 256)
    body = (
        "def handler(a):\n"
        "    hoard = []\n"
        "    while True:\n"
        "        hoard.append(bytearray(64 * 1024 * 1024))"
    )
    use_child(monkeypatch, with_command("hog", body))
    started = time.monotonic()
    with pytest.raises((sandbox.Refused, sandbox.SandboxError)):
        sandbox.run("hog", {}, timeout=20)
    assert time.monotonic() - started < 20  # stopped by the limit, not by the clock
    assert slots_free()
    # the server process itself is fine and can still do the real work
    monkeypatch.undo()


@pytest.mark.skipif(POSIX, reason="documents the platform without resource limits")
def test_without_the_resource_module_a_notice_is_logged_and_the_timeout_still_works(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "_noticed", False)
    with caplog.at_level("WARNING", logger="vibehealth"):
        with pytest.raises(sandbox.Refused):
            sandbox.run("check_upload", {"path": "missing.pdf", "kind": "pdf"})
    assert "resource limits" in caplog.text
    assert sandbox_child.apply_limits({"memory_bytes": 1 << 20}) == []


# --- how many children at once -----------------------------------------------------------------------------


def _counting_child(log_dir, seconds: float = 0.5):
    """Each child records when it ran, in a file of its own (appending to one shared file is not atomic
    on Windows)."""
    return child(
        "import os, time\n"
        "t0 = time.time()\n"
        f"time.sleep({seconds})\n"
        f"open(os.path.join({str(log_dir)!r}, str(os.getpid())), 'w').write(f'{{t0}} {{time.time()}}')\n"
        "import struct; m = b'{\"ok\": true}'\n"
        "os.write(1, b'VHSB1' + struct.pack('>II', len(m), 0) + m)"
    )


def intervals(log_dir) -> list[tuple[float, float]]:
    out = []
    for name in os.listdir(log_dir):
        a, b = (float(x) for x in (log_dir / name).read_text().split())
        out.append((a, b))
    return out


def peak(spans) -> int:
    """The most children that were running at the same moment."""
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans], key=lambda e: (e[0], e[1]))
    running = best = 0
    for _t, delta in events:
        running += delta
        best = max(best, running)
    return best


def test_at_most_two_children_run_at_once_across_threads(monkeypatch, tmp_path):
    log_dir = tmp_path
    use_child(monkeypatch, _counting_child(log_dir))
    errors = []

    def go():
        try:
            sandbox.run("check_upload", {"path": "x", "kind": "pdf"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(7)]
    started = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    spans = intervals(log_dir)
    assert not errors and len(spans) == 7
    assert peak(spans) == 2  # never more, and it did use both
    assert time.monotonic() - started >= 1.5  # 7 jobs of 0.5 s two at a time


def test_the_async_gate_limits_requests_from_several_event_loops(monkeypatch, tmp_path):
    """Each thread has its own loop (as with a test client per request): the gate is still one."""
    use_child(monkeypatch, _counting_child(tmp_path, 0.4))
    outcomes = []

    def go():
        outcomes.append(asyncio.run(sandbox.arun("check_upload", {"path": "x", "kind": "pdf"})).meta["ok"])

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes == [True] * 6 and peak(intervals(tmp_path)) == 2
    assert sandbox.GATE._free == sandbox.MAX_CONCURRENT and not sandbox.GATE._waiters


def test_a_cancelled_waiter_gives_its_place_back(monkeypatch, tmp_path):
    use_child(monkeypatch, _counting_child(tmp_path, 0.6))

    async def scenario():
        jobs = [asyncio.create_task(sandbox.arun("check_upload", {"path": "x", "kind": "pdf"})) for _ in range(5)]
        await asyncio.sleep(0.2)  # two run, three wait
        jobs[3].cancel()
        jobs[4].cancel()
        results = await asyncio.gather(*jobs, return_exceptions=True)
        return results

    results = asyncio.run(scenario())
    assert sum(isinstance(r, asyncio.CancelledError) for r in results) == 2
    assert sum(getattr(r, "meta", None) is not None for r in results) == 3
    time.sleep(0.2)
    assert sandbox.GATE._free == sandbox.MAX_CONCURRENT and not sandbox.GATE._waiters


# --- nothing is parsed in this process -------------------------------------------------------------------------


def test_the_server_process_never_parses_an_upload(monkeypatch):
    """Upload, thumbnail and reading work with pdfium and Pillow's file opening made to fail in THIS
    process: only the child (a separate interpreter) has them."""
    import pypdfium2
    from PIL import Image

    client.get("/api/status")
    data, jpeg = pdf(2), photo("JPEG", size=(300, 200))

    def boom(*a, **kw):
        raise AssertionError("parsed in the server process")

    monkeypatch.setattr(pypdfium2, "PdfDocument", boom)
    monkeypatch.setattr(Image, "open", boom)
    monkeypatch.setattr(Image, "frombytes", boom)
    ids = []
    for payload, name in ((data, "a.pdf"), (jpeg, "b.jpg")):
        r = client.post("/api/documents/upload", files={"file": (name, payload, "application/octet-stream")})
        assert r.status_code == 201, r.text
        ids.append(r.json()["document"]["id"])
    for did in ids:
        assert client.get(f"/api/documents/{did}/thumbnail").status_code == 200  # the cache made with the upload
        with Session(engine) as s:
            sha = s.get(Document, did).sha256
        os.remove(os.path.join(uploads.thumbs_root(), f"{sha}.jpg"))
        assert client.get(f"/api/documents/{did}/thumbnail").status_code == 200  # made again, by the sandbox
    from app import sources

    with Session(engine) as s:
        doc = s.get(Document, ids[0])
        pages = asyncio.run(sources.load_pages(sources.DocRef.of(doc)))[0]
    assert len(pages) == 2 and pages.png(1, 100).startswith(b"\x89PNG")


def test_module_of_the_upload_code_does_not_import_the_parsers():
    import app.uploads as module

    assert not hasattr(module, "pdfium") and not hasattr(module, "Image")
    src = open(module.__file__, encoding="utf-8").read()
    assert "import pypdfium2" not in src and "from PIL" not in src and "import PIL" not in src


# --- the pages the readers get: same as before -------------------------------------------------------------------


def _paperless_way(data: bytes, media: str, dpi: int) -> bytes:
    return render.Pages(data, media).png(0, dpi)


@pytest.mark.parametrize("dpi", [100, 150, 200])
def test_an_uploaded_pdf_page_is_byte_for_byte_what_the_in_process_path_made(tmp_path, dpi):
    data = pdf(2)
    path = tmp_path / "a.pdf"
    path.write_bytes(data)
    pages = sandbox.SandboxPages(str(path), "pdf")
    assert len(pages) == 2
    assert pages.png(0, dpi) == _paperless_way(data, "application/pdf", dpi)
    assert pages.png(1, dpi) == render.Pages(data, "application/pdf").png(1, dpi)


@pytest.mark.parametrize("fmt,size,orientation", [
    ("JPEG", (4000, 3000), 6), ("JPEG", (800, 600), None), ("PNG", (3000, 2500), None), ("WEBP", (500, 700), None),
])
@pytest.mark.parametrize("dpi", [100, 150, 200])
def test_an_uploaded_photo_page_is_byte_for_byte_what_the_old_chain_made(tmp_path, fmt, size, orientation, dpi):
    data = photo(fmt, size=size, orientation=orientation)
    path = tmp_path / "p"
    path.write_bytes(data)
    kind = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp"}[fmt]
    old = _paperless_way(render.prepare_image(data), "image/png", dpi)  # what _read_upload + Pages did
    assert sandbox.SandboxPages(str(path), kind).png(0, dpi) == old


def test_a_page_that_cannot_be_drawn_is_a_page_error_and_the_reading_goes_on(monkeypatch):
    from test_uploads_pipeline import FakeOllama, NoPaperless  # the fakes of the pipeline tests

    monkeypatch.setattr(reading, "Paperless", NoPaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    client.get("/api/status")
    did = client.post("/api/documents/upload", files={"file": ("a.pdf", pdf(2), "application/pdf")}).json()["document"]["id"]
    real = sandbox.SandboxPages.png

    def png(self, index, dpi):
        if index == 1:
            raise sandbox.SandboxError("timeout", "this file took too long to read")
        return real(self, index, dpi)

    monkeypatch.setattr(sandbox.SandboxPages, "png", png)
    summary = asyncio.run(reading.read_document(did, {}))
    assert summary["status"] == "done" and summary["pages"] == 2
    errors = summary["page_errors"]
    assert {(e["page"], e["reader"]) for e in errors} == {(2, "A"), (2, "B")}
    assert all("could not be drawn" in e["error"] for e in errors)


def test_a_hanging_child_ends_a_reading_with_an_error_run_not_a_crash(monkeypatch, fast):
    from test_uploads_pipeline import FakeOllama, NoPaperless

    monkeypatch.setattr(reading, "Paperless", NoPaperless)
    monkeypatch.setattr(reading, "Ollama", FakeOllama)
    client.get("/api/status")
    did = client.post("/api/documents/upload", files={"file": ("a.pdf", pdf(1), "application/pdf")}).json()["document"]["id"]
    use_child(monkeypatch, child("import time; time.sleep(600)"))
    with pytest.raises(Exception, match="could not be read"):
        asyncio.run(reading.read_document(did, {}))
    from app.models import ExtractionRun

    with Session(engine) as s:
        run = s.exec(select(ExtractionRun).where(ExtractionRun.document_id == did)).one()
    assert run.status == "error" and "took too long" in run.error and slots_free()
