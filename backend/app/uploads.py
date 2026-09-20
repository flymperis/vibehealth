"""Files uploaded by the user: what is accepted, how it is checked, where it is kept.

Layout under the data folder (all directories 0700, all files 0600):

    uploads/<xx>/<uuid4hex>.<ext>   the originals (xx = first two hex digits of the name)
    uploads/.tmp/<uuid4hex>         a file that is still arriving or being checked
    uploads/.pending-delete         paths to delete: noted before a delete commits, retried at the next start
    cache/thumbs/<sha256>.jpg       thumbnails, made in the sandbox at upload time (can always be rebuilt)

Nothing that comes from the client is ever used as a path: the folder, the name and the extension
are made here, and the extension comes from the bytes (magic numbers), not from the file name or
the Content-Type the browser sent. Those two are only ever shown, after `display_name` cleaned the
name. This folder holds medical originals: back it up together with the database.

The server never parses an uploaded file itself. Checking it, drawing the thumbnail and drawing the
pages for the readers happen in a short-lived child process (sandbox.py, sandbox_child.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from . import sandbox
from .config import get_settings
from .filelimits import (  # noqa: F401  (re-exported: the numbers live where the sandbox child can read them too)
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    MAX_PDF_PAGE_POINTS,
    MAX_PDF_PAGES,
    THUMB_SIDE,
)
from .models import DocumentKind

log = logging.getLogger("vibehealth")

# --- limits ---------------------------------------------------------------------------------------

MULTIPART_OVERHEAD = 64 * 1024  # boundaries, headers and the small text fields around the file
MAX_FIELD_BYTES = 4 * 1024  # a text field of the form (title, kind, doc_date, read_now)
MAX_TITLE = 200
MIN_DOC_YEAR = 1900
TMP_MAX_AGE = 3600  # seconds: an orphan in .tmp older than this is removed (at start, and every SWEEP_INTERVAL)
SWEEP_INTERVAL = 600  # seconds between two clean-ups of the scratch folder while the app runs

# How an upload may arrive. A body that trickles in (a client that stalls, or a slow-loris) would hold
# a temp file and a request open for as long as it likes: it is cut after IDLE_TIMEOUT without a
# byte, when its average speed after RATE_GRACE seconds is under MIN_RATE, or at the overall deadline.
IDLE_TIMEOUT = 30.0
RATE_GRACE = 15.0
MIN_RATE = 32 * 1024  # bytes per second (~ 256 kbit/s: any real connection, and a phone on 3G, is above)
MAX_RECEIVE_SECONDS = 2 * 3600
FLUSH_AT = 256 * 1024  # file bytes are written (and hashed) in a worker thread this much at a time

# How many uploads may be in flight at once (each holds a temp file of up to the size cap), and how
# much room the disk and the uploads folder must have.
MAX_INFLIGHT = 3
RETRY_AFTER = 5  # seconds, for the 429
FREE_SPACE_FACTOR = 2  # an upload starts only with this many times the size cap free on the disk

# What is accepted: sniffed name -> (stored media type, extension)
TYPES: dict[str, tuple[str, str]] = {
    "pdf": ("application/pdf", "pdf"),
    "jpeg": ("image/jpeg", "jpg"),
    "png": ("image/png", "png"),
    "webp": ("image/webp", "webp"),
}
ACCEPTED = "PDF, JPEG, PNG or WebP"


class UploadError(Exception):
    """A refusal with the HTTP status and a message that is safe to show."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers


# --- where things live ------------------------------------------------------------------------------


def _private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _base() -> str:
    """The uploads folder as a path: nothing is created (reading paths must not touch the disk)."""
    return os.path.join(os.path.abspath(get_settings().data_dir), "uploads")


def root() -> str:
    """The uploads folder, created (0700) if it is not there: for code that writes."""
    return _private_dir(_base())


def tmp_root() -> str:
    return _private_dir(os.path.join(root(), ".tmp"))


def thumbs_root() -> str:
    data = os.path.abspath(get_settings().data_dir)
    _private_dir(os.path.join(data, "cache"))
    return _private_dir(os.path.join(data, "cache", "thumbs"))


def stored_file(relative: str | None) -> str | None:
    """The absolute path of a stored original, or None. Whatever the string is, the answer is a
    path inside uploads/ (symbolic links resolved) or nothing: this is the guard against traversal
    for every read and delete."""
    if not relative or "\0" in relative or os.path.isabs(relative) or relative.startswith(("/", "\\")):
        return None
    # Stored names are generated ("ab/<uuid hex>.ext"): a backslash or a colon (a Windows separator or
    # drive letter) never occurs in them, and on Linux they would only be odd file names, so refuse
    # them on every platform instead of depending on how the OS reads them.
    if "\\" in relative or ":" in relative:
        return None
    base = os.path.realpath(_base())
    path = os.path.realpath(os.path.join(base, relative))
    try:
        inside = os.path.commonpath([base, path]) == base and path != base
    except ValueError:  # another drive (Windows)
        inside = False
    return path if inside else None


def file_exists(relative: str | None) -> bool:
    path = stored_file(relative)
    return bool(path) and os.path.isfile(path)


# --- what a name and the fields may look like -------------------------------------------------------

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_NAME_DROP = set('<>:"|?*\\/')


def _printable(text: str) -> str:
    """No control, format (bidi overrides) or unassigned characters, and no path separators."""
    return "".join(ch for ch in text if ch.isprintable() and ch not in _NAME_DROP)


def name_stem(raw: str | None) -> str:
    """A safe name to show, without an extension. Only its basename is taken (both separators), so
    `../../x` and `C:\\Windows\\x.pdf` are `x`; NUL and control characters are dropped, a Windows
    device name is prefixed, and it is cut to 100 characters. It is never used as a path."""
    name = unicodedata.normalize("NFC", raw or "")
    name = re.split(r"[\\/]", name)[-1]
    name = _printable(name).strip(" .")
    stem = os.path.splitext(name)[0].strip(" .")
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    return stem[:100].strip(" .") or "upload"


def display_name(raw: str | None, kind: str) -> str:
    """The stem plus the extension that the bytes say it is (not the one the client chose)."""
    return f"{name_stem(raw)}.{TYPES[kind][1]}"


def clean_title(raw: str) -> str:
    text = " ".join(_printable_text(raw).split())
    if not 1 <= len(text) <= MAX_TITLE:
        raise ValueError(f"must be 1 to {MAX_TITLE} characters")
    return text


def _printable_text(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", text) if ch.isprintable() or ch in " \t")


def check_doc_date(value: date) -> date:
    limit = date.today() + timedelta(days=1)
    if value.year < MIN_DOC_YEAR or value > limit:
        raise ValueError(f"must be between {MIN_DOC_YEAR}-01-01 and today")
    return value


def parse_kind(raw: str) -> DocumentKind:
    try:
        return DocumentKind(raw.strip().lower())
    except ValueError:
        raise UploadError(422, "kind must be one of: " + ", ".join(k.value for k in DocumentKind)) from None


def parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return check_doc_date(date.fromisoformat(raw))
    except ValueError:
        raise UploadError(
            422, f"doc_date must be a date like 2025-03-01, from {MIN_DOC_YEAR} to today"
        ) from None


def parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("", "0", "false", "no", "off"):
        return False
    raise UploadError(422, "read_now must be true or false")


# --- room: how many uploads at once, free disk, the size of the folder -------------------------------

_inflight = 0
_inflight_lock = threading.Lock()  # guards a counter, never held while anything waits or works


def begin_upload() -> int:
    """Take one of the MAX_INFLIGHT places. 429 (with Retry-After) when they are all taken. Returns how
    many other uploads are running now. Every successful call needs its `end_upload`."""
    global _inflight
    with _inflight_lock:
        if _inflight >= MAX_INFLIGHT:
            raise UploadError(
                429, "Too many uploads are being processed right now: try again in a few seconds.",
                {"Retry-After": str(RETRY_AFTER)},
            )
        _inflight += 1
        return _inflight - 1


def end_upload() -> None:
    global _inflight
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


def total_bytes() -> int:
    """The size of everything in uploads/ (originals and temp files). Walks the folder: uploads are few
    and this only runs for an upload and for /api/status."""
    total = 0
    stack = [_base()]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def free_bytes() -> int:
    return shutil.disk_usage(os.path.abspath(get_settings().data_dir)).free


def _mb(n: int) -> int:
    return -(-n // (1024 * 1024))


def check_room(cap: int, max_total: int, others: int = 0, declared: int | None = None) -> int:
    """Refuse (507) before reading a byte when the disk is too full or the uploads folder is at its
    quota. `others` are the uploads already in flight: each may still write up to `cap`. Returns how
    many bytes uploads/ holds now."""
    need = cap * (FREE_SPACE_FACTOR + others)
    try:
        free = free_bytes()
    except OSError:
        free = need  # cannot tell: do not refuse on a guess
    if free < need:
        raise UploadError(
            507, f"There is not enough free disk space to accept an upload (about {_mb(need)} MB must be free)."
        )
    used = total_bytes()
    if used >= max_total or (declared and used + declared > max_total):
        raise UploadError(
            507,
            f"The uploads folder is full ({_mb(used)} of {_mb(max_total)} MB used). "
            "Delete documents, or raise the limit in Settings.",
        )
    return used


# --- receiving: the body goes to disk while it arrives ------------------------------------------------


@dataclass
class Received:
    path: str  # in uploads/.tmp
    size: int
    sha256: str
    head: bytes  # the first bytes, for sniffing
    filename: str  # as the client wrote it: display only, after name_stem
    fields: dict[str, str] = field(default_factory=dict)


def cap_bytes(max_mb: int) -> int:
    return int(max_mb) * 1024 * 1024


def sniff(head: bytes) -> str | None:
    """The type from the magic bytes. The Content-Type and the file name play no part."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


# Text fields of an upload form and how long each may be (bytes). Another form that carries a file (the
# model check) passes its own to `receive`.
_TEXT_FIELDS = {"kind": MAX_FIELD_BYTES, "title": MAX_FIELD_BYTES, "doc_date": MAX_FIELD_BYTES,
                "read_now": MAX_FIELD_BYTES}

_active_tmp: set[str] = set()  # temp files of uploads in flight: the periodic sweep leaves them alone


class _Sink:
    """The file part on its way to disk. The parser's callbacks run on the event loop and only note what
    arrived (`add`); opening the file, writing and hashing are done by `write` in a worker thread, a
    slice (FLUSH_AT) at a time, so nothing blocks the loop and memory stays flat."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.hasher = hashlib.sha256()
        self.pending: list[bytes] = []
        self.pending_bytes = 0
        self.file = None
        self.begun = False  # the file part has started
        self.ending = False  # ... and has ended (all its data has been seen)
        self.closed = False
        self.aborted = False
        self.busy = False  # a `write` is (or may still be) running in a worker thread
        self.size = 0
        self.head = b""

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if len(self.head) < 16:
            self.head = (self.head + chunk)[:16]
        self.pending.append(chunk)
        self.pending_bytes += len(chunk)

    def due(self) -> bool:
        return self.pending_bytes >= FLUSH_AT or (self.ending and not self.closed)

    def take(self) -> list[bytes]:
        chunks, self.pending, self.pending_bytes = self.pending, [], 0
        return chunks

    def write(self, chunks: list[bytes], ending: bool) -> None:  # worker thread
        try:
            if not self.aborted:
                if self.file is None:
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                    self.file = os.fdopen(fd, "wb")
                for chunk in chunks:
                    self.hasher.update(chunk)
                    self.file.write(chunk)
                if ending:
                    self.file.close()
                    self.file = None
                    self.closed = True
        finally:
            if self.aborted:
                self.discard()

    def discard(self) -> None:
        try:
            if self.file is not None:
                self.file.close()
        except OSError:
            pass
        self.file = None
        remove_quietly(self.path)


async def _flush(sink: _Sink) -> None:
    chunks, ending = sink.take(), sink.ending
    sink.busy = True
    try:
        await asyncio.to_thread(sink.write, chunks, ending)
    except asyncio.CancelledError:
        raise  # the worker thread may still be writing: `busy` stays set, and it cleans up (see _abort)
    except BaseException:
        sink.busy = False  # the write has ended, with an error (a full disk): the caller cleans up
        raise
    sink.busy = False


def receive_deadline(max_bytes: int) -> float:
    return min(MAX_RECEIVE_SECONDS, 30 + max_bytes / MIN_RATE)


async def receive(
    request: Request, max_bytes: int, quota_left: int | None = None, text_fields: dict[str, int] | None = None
) -> Received:
    """Read a multipart/form-data body, writing the one `file` part to uploads/.tmp/<uuid> as it
    arrives and counting its bytes: past `max_bytes` it stops with 413 (past `quota_left`, with 507)
    without reading on. `text_fields` (name -> longest allowed, in bytes) says which other form fields are
    kept in `Received.fields`; the rest are read and dropped. Nothing is buffered whole and nothing goes to the system's temp folder; the
    disk work runs in a worker thread. A body that stalls or crawls is cut (408). On any failure the
    temporary file is removed."""
    content_type, params = parse_options_header(request.headers.get("content-type", "").encode("latin-1", "replace"))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadError(415, "send the file as multipart/form-data")

    allowed = _TEXT_FIELDS if text_fields is None else text_fields
    sink = _Sink(os.path.join(await asyncio.to_thread(tmp_root), uuid.uuid4().hex))  # (makes the folder: off the loop)
    _active_tmp.add(sink.path)
    filename = ""
    seen_file = False
    fields: dict[str, str] = {}
    part: dict = {}
    header_name = header_value = b""
    headers: dict[bytes, bytes] = {}

    def on_part_begin() -> None:
        nonlocal headers
        headers = {}
        part.clear()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        nonlocal header_name
        header_name += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        nonlocal header_value
        header_value += data[start:end]

    def on_header_end() -> None:
        nonlocal header_name, header_value
        headers[header_name.lower()] = header_value
        header_name = header_value = b""

    def on_headers_finished() -> None:
        nonlocal seen_file, filename
        _, disposition = parse_options_header(headers.get(b"content-disposition", b""))
        name = disposition.get(b"name", b"").decode("utf-8", "replace")
        raw_name = disposition.get(b"filename")
        part["name"] = name
        if name == "file":
            if raw_name is None:
                raise UploadError(422, "the file part has no file name: send it as a file field")
            if seen_file:
                raise UploadError(422, "send one file per upload")
            seen_file = True
            filename = raw_name.decode("utf-8", "replace")
            sink.begun = True
            part["kind"] = "file"
        elif name in allowed and raw_name is None:
            part["kind"] = "text"
            part["buf"] = b""
        else:
            part["kind"] = "ignore"  # unknown fields are read and dropped

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if part.get("kind") == "file":
            if sink.size + len(chunk) > max_bytes:
                raise UploadError(413, f"the file is larger than the limit of {max_bytes // (1024 * 1024)} MB")
            if quota_left is not None and sink.size + len(chunk) > quota_left:
                raise UploadError(507, "The uploads folder is full: there is no room left for this file. "
                                       "Delete documents, or raise the limit in Settings.")
            sink.add(chunk)
        elif part.get("kind") == "text":
            part["buf"] += chunk
            if len(part["buf"]) > allowed[part["name"]]:
                raise UploadError(422, f"the field {part['name']} is too long")

    def on_part_end() -> None:
        if part.get("kind") == "file":
            sink.ending = True
        elif part.get("kind") == "text":
            fields[part["name"]] = part["buf"].decode("utf-8", "replace")

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
        max_header_count=8,
    )
    started = time.monotonic()
    deadline = started + receive_deadline(max_bytes)
    arrived = 0
    try:
        chunks = request.stream().__aiter__()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise UploadError(408, "The upload took too long and was stopped.")
            try:
                async with asyncio.timeout(min(IDLE_TIMEOUT, left)):
                    chunk = await chunks.__anext__()
            except StopAsyncIteration:
                break
            except TimeoutError:
                if time.monotonic() >= deadline - 0.05:
                    raise UploadError(408, "The upload took too long and was stopped.") from None
                raise UploadError(408, "The upload stalled or was too slow and was stopped.") from None
            arrived += len(chunk)
            elapsed = time.monotonic() - started
            if elapsed > RATE_GRACE and arrived / elapsed < MIN_RATE:
                raise UploadError(408, "The upload was too slow and was stopped.")
            if parser.write(chunk) != len(chunk):
                raise UploadError(400, "the upload is not valid multipart/form-data")
            if sink.due():
                await _flush(sink)
        parser.finalize()
        if sink.begun and not sink.ending:  # the body ended inside the file part
            raise UploadError(400, "the upload was cut short")
        if sink.due():
            await _flush(sink)
        if not seen_file:
            raise UploadError(422, "no file was sent (field name: file)")
        if sink.size == 0:
            raise UploadError(422, "the file is empty")
    except UploadError:
        _abort(sink)
        raise
    except ClientDisconnect:
        _abort(sink)
        raise UploadError(400, "the upload was interrupted") from None
    except FormParserError:
        _abort(sink)
        raise UploadError(400, "the upload is not valid multipart/form-data") from None
    except BaseException:  # the body cap's 413, a cancelled request, a full disk: cleaned up, unchanged
        _abort(sink)
        raise
    return Received(sink.path, sink.size, sink.hasher.hexdigest(), sink.head, filename, fields)


def _abort(sink: _Sink) -> None:
    sink.aborted = True
    if not sink.busy:  # otherwise the write that is running cleans up when it ends
        sink.discard()


def remove_quietly(path: str) -> None:
    _active_tmp.discard(path)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove a temporary upload file")


# --- checking that it really is what it says: in the sandbox -------------------------------------------


@dataclass
class Checked:
    pages: int
    thumbnail: bytes  # a JPEG, made by the child together with the check


def _refusal(exc: sandbox.SandboxError) -> UploadError:
    if exc.kind == "busy":
        return UploadError(503, "The server is busy checking other files: try again in a moment.",
                           {"Retry-After": str(RETRY_AFTER)})
    reason = "it took too long to open" if exc.kind == "timeout" else "it could not be processed safely"
    return UploadError(422, f"This file could not be read: {reason}.")


async def check(path: str, kind: str) -> Checked:
    """Open the file for real, in a sandbox child (never in this process). Raises UploadError(422) for
    anything unusable, including a file that makes the child hang, crash or run out of memory."""
    try:
        result = await sandbox.arun("check_upload", {"path": path, "kind": kind})
    except sandbox.Refused as exc:
        raise UploadError(422, str(exc)) from None
    except sandbox.SandboxError as exc:
        raise _refusal(exc) from None
    pages = result.meta.get("pages")
    if not isinstance(pages, int) or pages < 1 or not result.payload.startswith(b"\xff\xd8\xff"):
        raise UploadError(422, "This file could not be read: it could not be processed safely.")
    return Checked(pages, result.payload)


# --- keeping the original -------------------------------------------------------------------------


def store(tmp_path: str, kind: str) -> str:
    """Move a checked file to uploads/<xx>/<uuid4hex>.<ext>; returns the path relative to uploads/
    (with forward slashes). The name is generated here."""
    name = uuid.uuid4().hex
    relative = f"{name[:2]}/{name}.{TYPES[kind][1]}"
    base = os.path.realpath(root())
    _private_dir(os.path.join(base, name[:2]))
    target = os.path.realpath(os.path.join(base, relative))
    if os.path.commonpath([base, target]) != base:  # defence in depth: cannot happen with a generated name
        raise UploadError(500, "could not store the file")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, target)
    _active_tmp.discard(tmp_path)
    return relative


# --- thumbnails ---------------------------------------------------------------------------------------


def _thumb_file(sha: str | None) -> str | None:
    return os.path.join(thumbs_root(), f"{sha}.jpg") if sha and re.fullmatch(r"[0-9a-f]{64}", sha) else None


def write_thumbnail(sha: str | None, data: bytes) -> bool:
    """Cache a thumbnail made by the sandbox. Best effort: without the cache it is made again."""
    try:
        cache = _thumb_file(sha)
        if not cache:
            return False
        tmp = cache + f".{uuid.uuid4().hex}.part"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, cache)
            return True
        except OSError:
            remove_quietly(tmp)
            return False
    except Exception:  # noqa: BLE001
        log.warning("could not cache a thumbnail", exc_info=True)
        return False


def read_thumbnail_cache(sha: str | None) -> bytes | None:
    """The cached thumbnail, or None when there is none (or it is not a JPEG: then it is made again)."""
    try:
        cache = _thumb_file(sha)
        if not cache or not os.path.isfile(cache):
            return None
        with open(cache, "rb") as f:
            data = f.read(8 * 1024 * 1024)
        return data if data.startswith(b"\xff\xd8\xff") else None
    except OSError:
        return None


async def thumbnail_bytes(relative: str | None, sha: str | None, kind: str) -> bytes | None:
    """What GET /thumbnail serves: the cached file. The cache is filled when the upload is made, so it
    is only missing after a restore or a manual clean-up; then the sandbox draws it again (waiting its
    turn at the gate, with the same limits as any other work), and it is cached. Nothing is ever drawn
    in this process. None when the original is gone or cannot be read."""

    def original_exists() -> bool:
        path = stored_file(relative)
        return bool(path) and os.path.isfile(path)

    if not await asyncio.to_thread(original_exists):
        return None
    cached = await asyncio.to_thread(read_thumbnail_cache, sha)
    if cached is not None:
        return cached
    path = await asyncio.to_thread(stored_file, relative)
    if not path or kind not in TYPES:
        return None
    async with sandbox.GATE:
        cached = await asyncio.to_thread(read_thumbnail_cache, sha)  # made by a request that was ahead of us
        if cached is not None:
            return cached
        try:
            result = await asyncio.to_thread(sandbox.run, "thumbnail", {"path": path, "kind": kind})
        except (sandbox.Refused, sandbox.SandboxError):
            log.warning("could not make a thumbnail")
            return None
    if not result.payload.startswith(b"\xff\xd8\xff"):
        return None
    await asyncio.to_thread(write_thumbnail, sha, result.payload)
    return result.payload


# --- deleting -------------------------------------------------------------------------------------------

_pending_lock = threading.Lock()


def _pending_file() -> str:
    return os.path.join(root(), ".pending-delete")


def _read_pending() -> list[str]:
    try:
        with open(_pending_file(), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def note_pending(relative: str | None) -> bool:
    """Write down that this file is to be deleted, BEFORE the database delete is committed: if the
    process dies between the commit and the unlink, the next start removes the file. (If the commit
    fails instead, the entry is harmless: the start-up sweep skips a path that a document owns.) Never
    raises. True when the entry is on disk."""
    if not relative:
        return True
    try:
        with _pending_lock:
            if relative in _read_pending():
                return True
            with open(_pending_file(), "a", encoding="utf-8") as f:
                f.write(relative + "\n")
        return True
    except Exception:  # noqa: BLE001
        log.warning("could not note an upload file for later removal")
        return False


def _forget_pending(relative: str | None) -> None:
    """The file is gone: take its entry off the list. Never raises."""
    if not relative:
        return
    try:
        with _pending_lock:
            left = [line for line in _read_pending() if line != relative]
            path = _pending_file()
            if left:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(left) + "\n")
            elif os.path.exists(path):
                os.remove(path)
    except Exception:  # noqa: BLE001
        pass


def delete_files(relative: str | None, sha: str | None, *, noted: bool = False) -> bool:
    """Remove the original and its cached thumbnail. NEVER raises: a file that cannot be removed is
    logged and kept in uploads/.pending-delete for the next start. `noted` says the caller wrote the
    entry already (before its commit): it is taken off the list once the file is gone. Returns
    whether everything went."""
    ok = True
    try:
        original = stored_file(relative)
        if original:
            try:
                os.remove(original)
                if noted:
                    _forget_pending(relative)
            except FileNotFoundError:
                if noted:
                    _forget_pending(relative)
            except OSError:
                ok = False
                log.warning("could not delete an uploaded file: it is left for the next clean-up")
                note_pending(relative)
    except Exception:  # noqa: BLE001 - path resolution or anything else: still not an error for the caller
        ok = False
        log.warning("could not delete an uploaded file: it is left for the next clean-up", exc_info=True)
        note_pending(relative)
    try:
        thumb = _thumb_file(sha)
        if thumb:
            try:
                os.remove(thumb)
            except FileNotFoundError:
                pass
            except OSError:
                ok = False  # only a cache: the start-up sweep drops thumbnails nobody owns
                log.warning("could not delete a cached thumbnail")
    except Exception:  # noqa: BLE001
        ok = False
        log.warning("could not delete a cached thumbnail", exc_info=True)
    return ok


# --- clean-up -------------------------------------------------------------------------------------------


def sweep_tmp() -> dict:
    """Remove what an upload that never finished left behind: files in .tmp older than TMP_MAX_AGE
    (one that is being received is left alone: it is in `_active_tmp`, and its age is that of its
    last write) and thumbnail scratch files (`.part`). Never raises."""
    result = {"tmp": 0, "parts": 0}
    cutoff = time.time() - TMP_MAX_AGE
    try:
        for entry in os.scandir(tmp_root()):
            try:
                if (entry.path not in _active_tmp and entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                    remove_quietly(entry.path)
                    result["tmp"] += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        log.warning("could not clean the upload scratch folder")
    try:
        for entry in os.scandir(thumbs_root()):
            try:
                if (entry.name.endswith(".part") and entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                    remove_quietly(entry.path)
                    result["parts"] += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return result


async def sweep_loop() -> None:
    """While the app runs: clean the scratch folder every SWEEP_INTERVAL seconds, not only at start."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            result = await asyncio.to_thread(sweep_tmp)
            if any(result.values()):
                log.info("upload clean-up: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("upload clean-up failed", exc_info=True)


def startup_sweep(known_hashes: set[str], known_paths: set[str]) -> dict:
    """At start: remove what an interrupted or failed upload left behind. Every step is on its own:
    a failing one is logged and the others still run; nothing here can stop the app from starting.
    - .tmp files older than an hour (a partly received file that nobody will finish),
    - files in .pending-delete (a delete that did not finish) unless a document owns them again,
    - cached thumbnails that belong to no upload.
    Original files that no document points at are NOT removed: after restoring an older database
    they are the only copy of a document."""
    result = {"tmp": 0, "pending": 0, "thumbs": 0}
    try:
        result["tmp"] = sweep_tmp()["tmp"]
    except Exception:  # noqa: BLE001
        log.warning("could not clean the upload scratch folder")

    try:
        wanted = list(dict.fromkeys(_read_pending()))
        if wanted:
            left = []
            for relative in wanted:
                if relative in known_paths:
                    continue  # a document has it again: not ours to delete
                try:
                    path = stored_file(relative)
                    if not path or not os.path.exists(path):
                        continue
                    os.remove(path)
                    result["pending"] += 1
                except Exception:  # noqa: BLE001
                    left.append(relative)
            with _pending_lock:
                pending = _pending_file()
                if left:
                    with open(pending, "w", encoding="utf-8") as f:
                        f.write("\n".join(left) + "\n")
                elif os.path.exists(pending):
                    os.remove(pending)
    except Exception:  # noqa: BLE001
        log.warning("could not process the list of files waiting for removal")

    try:
        for entry in os.scandir(thumbs_root()):
            try:
                stem = entry.name.removesuffix(".jpg")
                if entry.is_file(follow_symlinks=False) and stem not in known_hashes:
                    remove_quietly(entry.path)
                    result["thumbs"] += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return result


def sweep_at_start() -> dict:
    """`startup_sweep` with what the database knows about. Counts only are logged. Never raises."""
    try:
        from sqlmodel import Session, select

        from .db import engine
        from .models import Document, DocumentSource

        with Session(engine) as session:
            rows = session.exec(
                select(Document.sha256, Document.stored_path).where(Document.source == DocumentSource.UPLOAD)
            ).all()
        result = startup_sweep({r[0] for r in rows if r[0]}, {r[1] for r in rows if r[1]})
    except Exception:  # noqa: BLE001
        log.warning("the start-up clean-up of uploads failed; the app starts anyway", exc_info=True)
        return {"tmp": 0, "pending": 0, "thumbs": 0}
    if any(result.values()):
        log.info("upload clean-up: %s", result)
    return result


def kind_of_mime(mime: str | None) -> str | None:
    for name, (media, _ext) in TYPES.items():
        if media == mime:
            return name
    return None

