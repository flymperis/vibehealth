"""Run the parsing of an uploaded file in a short-lived child process (server side).

Why. PDFs and images from a person's upload are parsed by native code (pdfium, Pillow's codecs). A
bug in it, or a file built to make it loop or allocate without end, must cost at most one child
process, never the server. So the server never parses an uploaded file itself: it asks a child to
(see sandbox_child.py for the commands and the wire format).

What the server guarantees for every call (`run`):
  - a fresh `python` child per call (no state carries over from one file to the next);
  - a wall-clock timeout; when it runs out the child is killed with SIGKILL (its whole process group on
    POSIX) and the call fails cleanly;
  - on POSIX the child lowers its own limits before it opens the file: address space, CPU seconds,
    no core dump, few open files. On Windows (the development machine) there is no `resource` module:
    a notice is logged once and only the timeout applies;
  - the child's environment is scrubbed (no SECRET_KEY, no tokens, no password hash), stderr is
    dropped, its working directory is the system temp folder;
  - the answer is read through a pipe, in a reader thread, with a hard size cap (over it: the child is
    killed); it is parsed as a length-prefixed frame plus JSON. Nothing is ever unpickled;
  - at most MAX_CONCURRENT children run at any moment (a thread semaphore inside `run`, which also
    covers the reading loop), and `arun` puts an async gate in front so that requests wait on the
    event loop instead of parking worker threads.

Failure modes, all raised as SandboxError (safe to show): "timeout", "output" (over the size cap),
"crash" (any non-zero exit, a signal, no valid frame, the child could not start), "busy" (no free slot
for a long time). A file the child rejects on its merits is `Refused` (its message is ours).

Not a full sandbox: the child runs as the same user, with the same file system and network. What it
gives is containment of hangs, memory blow-ups and crashes, and a scrubbed environment; a
namespace/seccomp layer would be a further step (see docs/DESIGN.md).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field

from .ollama import SafeError

log = logging.getLogger("vibehealth")

MAX_CONCURRENT = 2
SLOT_WAIT_SECONDS = 180  # how long a call waits for a free slot before it gives up ("busy")

# Wall-clock budget per command (seconds) and the largest payload it may return (bytes).
TIMEOUTS = {"check_upload": 45, "thumbnail": 30, "page_count": 30, "render_page": 60}
DEFAULT_TIMEOUT = 45
PAYLOAD_CAPS = {"check_upload": 4 << 20, "thumbnail": 4 << 20, "page_count": 0, "render_page": 96 << 20}
DEFAULT_PAYLOAD_CAP = 4 << 20
MAX_META = 64 * 1024

# POSIX limits handed to the child. `VIBEHEALTH_SANDBOX_MEMORY_MB` sets the memory limit (0: none).
# It is an address-space limit (RLIMIT_AS) first; `selftest` steps down to RLIMIT_DATA (private writable
# memory: not fooled by an allocator that only *reserves* address space) and then to no memory limit
# if the child cannot run under the stricter one, so a limit can never make every upload fail.
MEMORY_MB = int(os.environ.get("VIBEHEALTH_SANDBOX_MEMORY_MB", "2048") or 0)
MEMORY_MODE = "as"  # "as" | "data" | "none"
CPU_MARGIN = 5  # CPU seconds allowed on top of the wall-clock timeout (a busy child uses < 1 s per second)
OPEN_FILES = 64

MAGIC = b"VHSB1"
_HEADER = struct.Struct(">II")

# The only environment variables the child gets: enough for Python and the platform, none of ours.
_ENV_KEEP = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
)

_BOOT = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from app import sandbox_child; sandbox_child.main()"
)


class SandboxError(SafeError):
    """The child could not do the job (hang, crash, over the output cap, no slot). `kind` says which."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Refused(SafeError):
    """The child looked at the file and says it is not acceptable (a fixed message of ours)."""


@dataclass
class Result:
    meta: dict
    payload: bytes = b""
    limits: list[str] = field(default_factory=list)


_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_noticed = False


# --- the child --------------------------------------------------------------------------------------


def _child_argv() -> list[str]:
    """How the child is started. (Tests replace this to run a misbehaving child.)"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-c", _BOOT.format(root=root)]


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.upper() in _ENV_KEEP}


def _limits(timeout: float) -> dict:
    limited = MEMORY_MB > 0 and MEMORY_MODE != "none"
    return {
        "memory_bytes": MEMORY_MB * 1024 * 1024 if limited else None,
        "memory_kind": MEMORY_MODE,
        "cpu_seconds": int(timeout) + CPU_MARGIN,
        "open_files": OPEN_FILES,
    }


def _kill(proc: subprocess.Popen) -> None:
    """End the child and everything it started. Safe to call twice and on a process that is gone (a
    process that has already been reaped is left alone: its pid may belong to someone else by now)."""
    try:
        if proc.poll() is not None:
            return
        if os.name == "posix":
            import signal

            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session (start_new_session)
            except ProcessLookupError:
                pass
            except PermissionError:
                proc.kill()
        else:
            proc.kill()
    except OSError:
        pass


def _notice_platform() -> None:
    global _noticed
    if _noticed:
        return
    _noticed = True
    try:
        import resource  # noqa: F401
    except ImportError:
        log.warning(
            "sandbox: resource limits (memory, CPU) are not available on this platform; "
            "only the wall-clock timeout protects the server from a hostile file"
        )


def _parse(raw: bytes) -> tuple[dict, bytes]:
    head = len(MAGIC) + _HEADER.size
    if len(raw) < head or raw[: len(MAGIC)] != MAGIC:
        raise SandboxError("crash", "this file could not be read")
    meta_len, payload_len = _HEADER.unpack(raw[len(MAGIC): head])
    if meta_len > MAX_META or head + meta_len + payload_len != len(raw):
        raise SandboxError("crash", "this file could not be read")
    try:
        meta = json.loads(raw[head: head + meta_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SandboxError("crash", "this file could not be read") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("ok"), bool):
        raise SandboxError("crash", "this file could not be read")
    return meta, raw[head + meta_len:]


def _clean_message(text: object) -> str:
    if not isinstance(text, str):
        return "this file could not be read"
    return "".join(ch for ch in text[:300] if ch.isprintable()) or "this file could not be read"


def _run_child(command: str, args: dict, timeout: float, payload_cap: int) -> Result:
    request = json.dumps({"cmd": command, "args": args, "limits": _limits(timeout)}).encode("utf-8") + b"\n"
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            _child_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_child_env(), cwd=tempfile.gettempdir(), bufsize=0, close_fds=True, **kwargs,
        )
    except (OSError, ValueError):
        log.warning("sandbox: could not start the child process", exc_info=True)
        raise SandboxError("crash", "this file could not be read") from None

    cap = len(MAGIC) + _HEADER.size + MAX_META + payload_cap
    received = bytearray()
    over = False

    def pump() -> None:
        nonlocal over
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                received.extend(chunk)
                if len(received) > cap:
                    over = True
                    _kill(proc)
                    return
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=pump, name="vibehealth-sandbox-reader", daemon=True)
    timed_out = False
    try:
        reader.start()
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except OSError:
            pass  # the child is already gone: its exit status tells
        reader.join(timeout)
        if reader.is_alive():
            timed_out = True
            _kill(proc)
            reader.join(5)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = proc.wait()
    finally:
        _kill(proc)  # an exception or a cancelled caller must not leave a child behind
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
        proc.wait()

    if timed_out:
        log.warning("sandbox: %s did not finish in %ss and was killed", command, timeout)
        raise SandboxError("timeout", "this file took too long to read")
    if over:
        log.warning("sandbox: %s returned more than %s bytes and was killed", command, cap)
        raise SandboxError("output", "this file could not be read: the result is too large")
    if code != 0:
        log.warning("sandbox: %s ended abnormally (exit status %s)", command, code)
        raise SandboxError("crash", "this file could not be read")
    meta, payload = _parse(bytes(received))
    limits = meta.get("limits") if isinstance(meta.get("limits"), list) else []
    if not meta["ok"]:
        raise Refused(_clean_message(meta.get("message")))
    if len(payload) > payload_cap:
        raise SandboxError("output", "this file could not be read: the result is too large")
    return Result(meta, payload, [str(x) for x in limits])


def run(command: str, args: dict, *, timeout: float | None = None, max_payload: int | None = None) -> Result:
    """Do `command` in a child and return its answer. Blocking: call it from a worker thread (or use
    `arun`). Raises Refused (the file is not acceptable) or SandboxError (the attempt failed)."""
    timeout = TIMEOUTS.get(command, DEFAULT_TIMEOUT) if timeout is None else timeout
    cap = PAYLOAD_CAPS.get(command, DEFAULT_PAYLOAD_CAP) if max_payload is None else max_payload
    _notice_platform()
    if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise SandboxError("busy", "the server is busy reading other files: try again in a moment")
    try:
        return _run_child(command, args, timeout, cap)
    finally:
        _slots.release()


# --- a check at start that the child can work under its limits ----------------------------------------


def _tiny_pdf() -> bytes:
    content = b"BT /F1 12 Tf 20 100 Td (VibeHealth) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def _tiny_png() -> bytes:
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * 8 for _ in range(8))  # 8 x 8, white
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def selftest() -> bool:
    """At start: can the child check a small PDF and a small PNG under the limits it is given? If not,
    step the memory limit down (address space -> data segment -> none) until it can, and say so loudly:
    a limit that breaks pdfium must not turn into "every upload fails". Returns whether the sandbox works.
    Never raises."""
    global MEMORY_MODE
    original = MEMORY_MODE
    ladder = [original, *(m for m in ("data", "none") if m != original)] if MEMORY_MB > 0 else ["none"]
    try:
        with tempfile.TemporaryDirectory(prefix="vibehealth-sandbox-") as folder:
            pdf_path, png_path = os.path.join(folder, "a.pdf"), os.path.join(folder, "a.png")
            with open(pdf_path, "wb") as f:
                f.write(_tiny_pdf())
            with open(png_path, "wb") as f:
                f.write(_tiny_png())
            for mode in ladder:
                MEMORY_MODE = mode
                try:
                    run("check_upload", {"path": pdf_path, "kind": "pdf"}, timeout=30)
                    run("check_upload", {"path": png_path, "kind": "png"}, timeout=30)
                except (SandboxError, Refused):
                    continue
                if mode != ladder[0]:
                    log.error(
                        "sandbox: the child could not run with the memory limit '%s' (%s MB); it now runs with "
                        "'%s'. Set VIBEHEALTH_SANDBOX_MEMORY_MB to a larger value (or 0 for none) to silence this.",
                        ladder[0], MEMORY_MB, mode,
                    )
                log.info("sandbox self-test passed (memory limit: %s)", mode if MEMORY_MB > 0 else "none")
                return True
    except Exception:  # noqa: BLE001
        log.exception("sandbox self-test could not run")
    MEMORY_MODE = original
    log.error("sandbox self-test FAILED: uploads will be refused until the sandbox child can start "
              "(check that `%s` can import pypdfium2 and Pillow)", sys.executable)
    return False


# --- the async gate in front of it ------------------------------------------------------------------


class AsyncGate:
    """A counting gate for coroutines that works across event loops and threads (a plain
    asyncio.Semaphore is bound to one loop). Waiting is done by awaiting a future, so a queued
    request costs no thread. The internal lock only guards a few pointer moves and is never held
    while waiting or working."""

    def __init__(self, permits: int) -> None:
        self._free = permits
        self._waiters: collections.deque[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = collections.deque()
        self._lock = threading.Lock()

    async def __aenter__(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._free > 0 and not self._waiters:
                self._free -= 1
                return
            future = loop.create_future()
            self._waiters.append((loop, future))
        try:
            await future
        except BaseException:
            with self._lock:
                try:
                    self._waiters.remove((loop, future))
                    still_queued = True
                except ValueError:
                    still_queued = False
            if not still_queued and future.done() and not future.cancelled():
                self.release()  # the slot had been handed to us just as we were cancelled
            raise

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        with self._lock:
            while self._waiters:
                loop, future = self._waiters.popleft()
                if loop.is_closed():
                    continue
                try:
                    loop.call_soon_threadsafe(self._grant, future)
                    return  # the slot goes straight to that waiter
                except RuntimeError:  # its loop closed in between
                    continue
            self._free += 1

    def _grant(self, future: asyncio.Future) -> None:
        if future.done():  # the waiter gave up: pass the slot on
            self.release()
        else:
            future.set_result(None)


GATE = AsyncGate(MAX_CONCURRENT)


async def arun(command: str, args: dict, **kwargs) -> Result:
    """`run` for a coroutine: waits its turn at the gate without holding a thread, then runs the
    blocking part in a worker thread."""
    async with GATE:
        return await asyncio.to_thread(run, command, args, **kwargs)


# --- pages for the reading pipeline -----------------------------------------------------------------


class SandboxPages:
    """The same interface as render.Pages (`len`, `png`, `close`) for a file stored by an upload, with
    every page drawn by a child. Each call is one short-lived child: a PDF is opened again for every
    page, which costs a fraction of a second and keeps nothing alive between pages. Call it from a
    worker thread. A failure is a SandboxError / Refused for that call only."""

    def __init__(self, path: str, kind: str) -> None:
        self.path, self.kind = path, kind
        self._count = 1
        if kind == "pdf":
            pages = run("page_count", {"path": path}).meta.get("pages")
            if not isinstance(pages, int) or not 1 <= pages <= 10_000:
                raise SandboxError("crash", "this file could not be read")
            self._count = pages

    def __len__(self) -> int:
        return self._count

    def png(self, index: int, dpi: int) -> bytes:
        data = run("render_page", {"path": self.path, "kind": self.kind, "index": index, "dpi": dpi}).payload
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise SandboxError("crash", "this page could not be drawn")
        return data

    def close(self) -> None:
        pass
