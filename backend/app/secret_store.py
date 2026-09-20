"""Secrets at rest, and the keys derived for the app.

(Not called `secrets.py`: a module of that name inside the package would shadow
the standard library's `secrets` for anything run from this directory.)

Keys
- One root secret: the `SECRET_KEY` environment variable when it is set, else the
  random contents of `<data dir>/.keys/master.key`, created on first use (file
  mode 0600, directory 0700; where that is not possible, e.g. Windows, a warning
  is logged and the app carries on).
- Purpose-specific keys come out of the root with HKDF and a label, so the key
  that encrypts stored secrets is unrelated to the one that signs sessions.

Encrypting
- Fernet. A stored secret looks like `enc:v1:<token>`.
- Anything that cannot be decrypted (a different key, damaged data) reads as
  "not set, please re-enter". It never raises.

Logging
- `install_log_redaction()` makes every log record, whatever handler prints it,
  mask the secrets the app has handled and any `enc:v1:` token.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets as _stdlib_secrets
import threading

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings

log = logging.getLogger("vibehealth")

PREFIX = "enc:v1:"
LABEL_ENCRYPTION = b"vibehealth/v1/secrets-encryption"
LABEL_SESSION = b"vibehealth/v1/session-signing"
_SALT = b"vibehealth-hkdf-salt-v1"
_KEY_FILE = "master.key"

_lock = threading.Lock()
_root: bytes | None = None


def reset_cache() -> None:
    """Forget the loaded root key (tests, and after the environment changed)."""
    global _root
    with _lock:
        _root = None


# --- the root key -----------------------------------------------------------


def key_file_path() -> str:
    return os.path.join(get_settings().data_dir, ".keys", _KEY_FILE)


def _read_key_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read().strip()


def _publish(tmp: str, path: str) -> bool:
    """Put the finished file `tmp` at `path` without ever overwriting: True when ours is the key now.
    A hard link is atomic and refuses to replace; where the filesystem has none, O_EXCL does."""
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    except OSError:  # no hard links here
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as f, open(tmp, "rb") as src:
            f.write(src.read())
        return True


def _create_key_file(path: str) -> bytes:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        log.warning("could not restrict permissions of %s", directory)
    material = base64.urlsafe_b64encode(_stdlib_secrets.token_bytes(32))
    # The key is written in full to a private file first and then published under its real name
    # without overwriting: no reader ever sees half a key, and a key that exists is never truncated.
    tmp = f"{path}.{os.getpid()}.{_stdlib_secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(material)
        if not _publish(tmp, path):
            existing = _read_key_file(path)  # another process got there first...
            if existing:
                return existing
            if os.path.getsize(path) == 0:  # ...or an empty leftover (nothing was ever encrypted with it)
                os.replace(tmp, path)
                tmp = ""
            else:
                return _read_key_file(path)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if os.name == "nt":
        log.warning("master key file permissions cannot be restricted on Windows: %s", path)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            log.warning("could not restrict permissions of the master key file")
    log.info("created a new master key file (%s)", _KEY_FILE)
    return material


def _root_key() -> bytes:
    global _root
    with _lock:
        if _root is None:
            env_key = get_settings().secret_key
            if env_key:
                _root = env_key.encode()
            else:
                path = key_file_path()
                material = _read_key_file(path) if os.path.exists(path) else b""
                if not material:
                    material = _create_key_file(path)
                _root = material
            register_secret(_root.decode(errors="ignore"))
        return _root


def key_source() -> str:
    """Where the root key comes from: 'env' (SECRET_KEY) or 'file' (master.key)."""
    return "env" if get_settings().secret_key else "file"


def ensure_key() -> None:
    """Create the key file now rather than at the first secret (startup)."""
    _root_key()


def derive(label: bytes, length: int = 32) -> bytes:
    """A key for one purpose, derived from the root with HKDF-SHA256."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=_SALT, info=label).derive(_root_key())


def session_signing_key() -> bytes:
    return derive(LABEL_SESSION)


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive(LABEL_ENCRYPTION)))


# --- encrypting -------------------------------------------------------------


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(PREFIX)


def encrypt(plain: str) -> str:
    register_secret(plain)
    return PREFIX + _fernet().encrypt(plain.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    """The plaintext, or None when it is missing or cannot be decrypted."""
    if not is_encrypted(stored):
        return None
    try:
        plain = _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        return None
    register_secret(plain)
    return plain


def last4(plain: str) -> str:
    """Enough to recognise a token in the UI, but only for long ones."""
    return plain[-4:] if len(plain) >= 12 else ""


# --- keeping secrets out of logs --------------------------------------------

_known: set[str] = set()
_TOKEN_RX = re.compile(re.escape(PREFIX) + r"[A-Za-z0-9_=\-]+")
_MIN_LEN = 4


def register_secret(value: str | None) -> None:
    if value and len(value) >= _MIN_LEN:
        _known.add(value)


MIN_SECRET_KEY_LENGTH = 16


def register_env_secrets() -> None:
    """At startup: the secrets that come from the environment are masked in logs from the first
    line on (the ones from the database are registered as they are read). A SECRET_KEY that is
    short is weak key material: say so, without saying what it is."""
    s = get_settings()
    for value in (s.paperless_token, s.app_password_hash, s.secret_key):
        register_secret(value)
    if s.secret_key and len(s.secret_key) < MIN_SECRET_KEY_LENGTH:
        log.warning(
            "SECRET_KEY is only %d characters long: use at least %d random characters "
            "(for example the output of `openssl rand -base64 32`)",
            len(s.secret_key), MIN_SECRET_KEY_LENGTH,
        )


def redact(text: str) -> str:
    for value in sorted(_known, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "***")
    return _TOKEN_RX.sub("enc:v1:***", text)


def _mask(record: logging.LogRecord) -> None:
    """Rewrite the record only when it holds a secret: other loggers (uvicorn's access
    log) format from `record.args` themselves, so those must stay as they are."""
    try:
        message = record.getMessage()
        masked = redact(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            text = logging.Formatter().formatException(record.exc_info)
            if redact(text) != text:
                record.exc_text = redact(text)
    except Exception:  # noqa: BLE001 - never let logging fail the request
        pass


class RedactionFilter(logging.Filter):
    """For handlers: masks the message of any record that reaches them."""

    def filter(self, record: logging.LogRecord) -> bool:
        _mask(record)
        return True


_installed = False


def install_log_redaction() -> None:
    """Wrap the log record factory: masks messages and tracebacks for every handler."""
    global _installed
    if _installed:
        return
    _installed = True
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        _mask(record)
        return record

    logging.setLogRecordFactory(factory)
