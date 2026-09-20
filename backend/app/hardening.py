"""Start-up hardening: a private data folder, and the one-worker rule.

The data folder holds medical records, the uploaded originals and the key that decrypts the saved
secrets. Nothing in it is meant for another user of the machine: the process umask is 077 before any
file is made, and folders/files that already exist are brought to 0700 / 0600. POSIX only: Windows
permissions do not work this way and the call does nothing there. Every step tolerates a failure
(a mounted volume that refuses chmod, say) with a warning and goes on.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence

from .config import get_settings

log = logging.getLogger("vibehealth")

_POSIX = os.name == "posix"


def apply_umask() -> None:
    """Files made from here on are private (0600) and folders 0700, whatever the launcher's umask was.
    Call before the first file is created."""
    if _POSIX:
        os.umask(0o077)


def _chmod(path: str, mode: int, problems: list[str]) -> None:
    try:
        if (os.stat(path).st_mode & 0o777) != mode:
            os.chmod(path, mode)
    except OSError:
        problems.append(path)


def secure_data_dir(data_dir: str | None = None) -> list[str]:
    """chmod 0700 the data folder and uploads/, cache/, backups/, .keys/ (with what is below uploads/
    and cache/), and 0600 the SQLite files (.db, -wal, -shm) and the files of backups/ and .keys/.
    Returns the paths that could not be changed (also logged, by count, never with content)."""
    if not _POSIX:
        return []
    data = os.path.abspath(data_dir or get_settings().data_dir)
    problems: list[str] = []
    if not os.path.isdir(data):
        return problems
    _chmod(data, 0o700, problems)
    for name in ("uploads", "cache", "backups", ".keys"):
        top = os.path.join(data, name)
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        for base, dirs, files in os.walk(top, followlinks=False):
            _chmod(base, 0o700, problems)
            for file in files:
                path = os.path.join(base, file)
                if not os.path.islink(path):
                    _chmod(path, 0o600, problems)
    db = os.path.join(data, os.path.basename(get_settings().database_path))
    for suffix in ("", "-wal", "-shm"):
        if os.path.isfile(db + suffix):
            _chmod(db + suffix, 0o600, problems)
    if problems:
        log.warning("could not make %d path(s) in the data folder private (mode 0700/0600)", len(problems))
    return problems


# --- one worker ------------------------------------------------------------------------------------------


def declared_workers(environ: Mapping[str, str] | None = None, argv: Sequence[str] | None = None) -> int:
    """How many server workers the launch asks for, as far as it can be seen: WEB_CONCURRENCY /
    UVICORN_WORKERS, and `--workers N` / `--workers=N` / `-w N` on the command line. 1 when nothing says more."""
    environ = os.environ if environ is None else environ
    argv = sys.argv if argv is None else argv
    found = [1]
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        try:
            found.append(int(environ.get(name, "").strip() or 1))
        except ValueError:
            pass
    for i, arg in enumerate(argv):
        value = None
        if arg in ("--workers", "-w") and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--workers="):
            value = arg.split("=", 1)[1]
        try:
            if value is not None:
                found.append(int(value))
        except ValueError:
            pass
    return max(found)


def check_single_worker() -> None:
    """VibeHealth keeps state in the process: the reading queue and its progress, the login throttle,
    the settings cache, the sandbox gate. With more than one worker each has its own copy, so the queue
    and the limits stop meaning what they say. Refuse to start (RuntimeError, one CRITICAL line) unless
    VIBEHEALTH_ALLOW_MULTIPLE_WORKERS=1 turns the refusal into a warning."""
    workers = declared_workers()
    if workers <= 1:
        return
    message = (
        f"{workers} workers were asked for (WEB_CONCURRENCY / --workers), but VibeHealth must run as ONE "
        "process: the reading queue, the login throttle and the upload limits live in memory. "
        "Run a single worker."
    )
    if os.environ.get("VIBEHEALTH_ALLOW_MULTIPLE_WORKERS", "").strip() == "1":
        log.warning("%s (continuing: VIBEHEALTH_ALLOW_MULTIPLE_WORKERS=1)", message)
        return
    log.critical("%s Refusing to start.", message)
    raise RuntimeError(message)
