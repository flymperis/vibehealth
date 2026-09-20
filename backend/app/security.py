"""One app password, one signed session cookie.

- The password hash comes from the app (saved when the password is changed in
  Settings) or, as a fallback, the `APP_PASSWORD_HASH` variable (make one with
  `python -m app.security`). With neither, the app is unusable until a password is set
  (setup_state.py) unless the operator sets `VIBEHEALTH_LEGACY_OPEN=1`, which keeps a
  password-less installation open (not recommended).
- New hashes use scrypt; PBKDF2 hashes made by earlier versions still verify and are
  upgraded at the next login. A saved hash that cannot be read locks the app.
- While no password is set, the FIRST password needs the one-time setup code (log and
  `<data dir>/.setup-code`); `python -m app.security reset-password` is the way back in.
- The session cookie is signed with a key derived from the master secret (see
  secret_store.py) and carries a session epoch, a fingerprint of the password's salt and
  its own id: a password change, a rotated APP_PASSWORD_HASH, logout (this token) and
  logout-all (every token) end sessions.
- Browsers are kept from sending state-changing requests from other sites by
  comparing the Origin header with the Host and refusing `Sec-Fetch-Site: cross-site`
  (`OriginCheckMiddleware`); DNS rebinding by an allowlist of Host values
  (`HostGuardMiddleware`).
- Guesses go through the login throttle (throttle.py) before they are checked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import Cookie, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import secret_store
from .config import get_settings
from .throttle import client_key, login_throttle

log = logging.getLogger("vibehealth")

COOKIE_NAME = "vibehealth_session"
MIN_PASSWORD_LENGTH = 10  # for new passwords only: hashes made earlier are unaffected
MAX_PASSWORD_LENGTH = 256

# New hashes: N=2^15, r=8, p=3, one of the OWASP Password Storage Cheat Sheet's equal-strength
# scrypt profiles (about 0.2 s and 32 MiB per check on a small server). A hash made with lower
# settings, or with PBKDF2, still verifies and is replaced by a current one at the next login.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 3
_SCRYPT_MAX_N = 2**17  # what a stored hash may ask of us when verifying
# (hashes from earlier versions, pbkdf2_sha256$<iterations>$..., still verify)


# --- password hashes ------------------------------------------------------------


# scrypt needs 128*N*r bytes (32 MiB at the defaults) for every run: at most this many at once,
# so a burst of logins cannot exhaust the memory of a small server.
MAX_CONCURRENT_SCRYPT = 2
_scrypt_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCRYPT)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _scrypt_slots:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32,
                              maxmem=128 * r * (n + p + 2) + (1 << 20))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, *rest = stored.split("$")
        if algorithm == "scrypt":
            n, r, p, salt_b64, digest_b64 = rest
            n, r, p = int(n), int(r), int(p)
            if not (2 <= n <= _SCRYPT_MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
                return False
            digest = _scrypt(password, base64.b64decode(salt_b64), n, r, p)
        elif algorithm == "pbkdf2_sha256":
            iterations, salt_b64, digest_b64 = rest
            if not 1 <= int(iterations) <= 10_000_000:
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
            )
        else:
            return False
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def _parts(stored: str) -> list[str]:
    return stored.split("$")


def _salt_of(stored: str) -> str:
    """The salt of a stored hash, as written in it (the whole string when it is not one we know)."""
    parts = _parts(stored)
    try:
        if parts[0] == "scrypt":
            return parts[4]
        if parts[0] == "pbkdf2_sha256":
            return parts[2]
    except IndexError:
        pass
    return stored


def needs_rehash(stored: str) -> bool:
    """True for a hash that verifies but is weaker than what new hashes get: PBKDF2, or scrypt with
    a lower N, r or p. (A stronger one, e.g. made by hand for APP_PASSWORD_HASH, is left alone.)"""
    parts = _parts(stored)
    if parts[0] == "pbkdf2_sha256":
        return True
    if parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return False
    return n < _SCRYPT_N or r < _SCRYPT_R or p < _SCRYPT_P


def _store():
    # Imported on use: `python -m app.security` only needs the hashing above and
    # must not open (or create) the database.
    from . import settings_store

    return settings_store


# --- password and epoch state -----------------------------------------------------


LOCKED_HASH = "locked$unreadable"  # stands in for a saved hash that cannot be read: nothing verifies
_last_locked_log = -1e9


def password_hash() -> str:
    """Saved in the app, else APP_PASSWORD_HASH, else empty (no password).

    Fails closed: when ANY saved `auth` value (the hash, the session epoch, the revoked-session list)
    is in the database but cannot be read, the app must not fall back to defaults ("no password",
    epoch 0, nothing revoked): that would open it or revive ended sessions. It is locked instead
    (login always fails) until `python -m app.security reset-password` is run on the server."""
    resolved = _store().resolve("auth")
    if resolved.broken:
        global _last_locked_log
        if time.monotonic() - _last_locked_log > 60:
            _last_locked_log = time.monotonic()
            log.error(
                "a saved value under auth (%s) cannot be read from the database: the app is "
                "LOCKED. Run `python -m app.security reset-password` on the server to recover.",
                ", ".join(sorted(resolved.broken)),
            )
        return LOCKED_HASH
    return resolved.values["password_hash"]


def legacy_open() -> bool:
    """VIBEHEALTH_LEGACY_OPEN=1: an installation without a password stays open, as installs were before
    passwords could be set in the app. Explicit, never inferred; not recommended."""
    return os.environ.get("VIBEHEALTH_LEGACY_OPEN", "").strip().lower() in ("1", "true", "yes", "on")


def maybe_rehash(password: str, verified_hash: str) -> None:
    """After a successful login: replace a weaker saved hash by a current one (same password, same
    salt, so sessions stay valid). Only a hash saved in the app: one from APP_PASSWORD_HASH is
    for the environment to change. Never fails the login."""
    if not needs_rehash(verified_hash):
        return
    try:
        with password_lock:
            store = _store()
            if store.resolve("auth", use_cache=False).sources["password_hash"] != "app":
                return
            if password_hash() != verified_hash:  # changed meanwhile: leave it
                return
            salt = base64.b64decode(_salt_of(verified_hash))
            store.update("auth", {"password_hash": hash_password(password, salt)})
        log.info("upgraded the stored password hash to the current scrypt settings")
    except Exception:  # noqa: BLE001 - a failed upgrade is retried at the next login
        log.warning("could not upgrade the stored password hash", exc_info=True)


def session_epoch() -> int:
    return _store().value("auth", "session_epoch")


def set_password(new_password: str) -> None:
    """Store a new hash and end every session that exists."""
    with password_lock:
        _store().update(
            "auth",
            {"password_hash": hash_password(new_password), "session_epoch": session_epoch() + 1},
        )
        clear_setup_code()  # a code is only for the first password


# --- setup code: the key to setting the FIRST password ------------------------------
#
# While no password is set the app is open, so anyone who can reach it could set one and
# lock the owner out. Setting the first password therefore needs a one-time code that
# only somebody with access to the server can read: it is printed in the log at startup
# and saved in `<data dir>/.setup-code` (mode 0600). The file is the source of truth (the
# CLI `reset-password` writes it too), it is removed once a password is set, and a new one
# is made at every start while there is still no password.

SETUP_CODE_LENGTH = 8
_SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O and 1/I: easy to read off a log
password_lock = threading.RLock()  # one password change / first-password claim at a time


def setup_code_path() -> str:
    return os.path.join(get_settings().data_dir, ".setup-code")


def _write_setup_code(code: str) -> None:
    path = setup_code_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.remove(path)  # so the new file is created with 0600, whatever the old one had
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(code + "\n")
    if os.name == "nt":
        log.warning("setup code file permissions cannot be restricted on Windows: %s", path)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            log.warning("could not restrict permissions of the setup code file")


def new_setup_code(announce: bool = True) -> str:
    """Make and save a fresh code (the old one stops working)."""
    with password_lock:
        code = "".join(secrets.choice(_SETUP_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
        _write_setup_code(code)
    if announce:
        log.warning(
            "Setup code: %s  (needed to set the first password; also saved in %s)",
            code, setup_code_path(),
        )
    return code


def _read_setup_code() -> str:
    try:
        with open(setup_code_path(), encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeError):
        return ""


def current_setup_code() -> str:
    """The code that is valid now; a new one is made when the file is missing or unreadable."""
    with password_lock:
        return _read_setup_code() or new_setup_code()


def clear_setup_code() -> None:
    try:
        os.remove(setup_code_path())
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove the setup code file")


def ensure_setup_code() -> None:
    """At startup: no password -> a new code (logged and saved); a password -> no code file."""
    if password_hash():
        clear_setup_code()
    else:
        new_setup_code()


def check_setup_code(candidate: str) -> bool:
    expected = current_setup_code()
    given = "".join(candidate.split()).upper()  # case and stray spaces do not matter
    return hmac.compare_digest(given.encode(), expected.encode())


# --- session cookie ------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_store.session_signing_key(), salt="session")


def _fingerprint(stored_hash: str) -> str:
    """A short keyed fingerprint of the password hash in force. A token carries it, so a token
    made under another password (a change in the app, or a new APP_PASSWORD_HASH) stops working.
    Keyed, so the token does not reveal anything about the hash."""
    # Of the salt, not the whole hash: a hash upgrade at login (see maybe_rehash) keeps the salt,
    # so it does not end anybody's session, while a new password or a new APP_PASSWORD_HASH
    # (new salt) does.
    salt = _salt_of(stored_hash).encode()
    mac = hmac.new(secret_store.session_signing_key(), b"pw-fingerprint:" + salt, "sha256")
    return mac.hexdigest()[:12]


def make_session_token() -> str:
    return _serializer().dumps({
        "v": 2,
        "e": session_epoch(),  # bumped by a password change and by "sign out everywhere"
        "h": _fingerprint(password_hash()),
        "j": secrets.token_hex(8),  # this token's id, so signing out can revoke just this one
    })


def _read_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=get_settings().session_days * 86400)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def valid_session(token: str | None) -> bool:
    data = _read_token(token)
    if data is None or data.get("e", 0) != session_epoch():
        return False
    stored = password_hash()
    if not isinstance(data.get("h"), str) or not hmac.compare_digest(data["h"], _fingerprint(stored)):
        return False
    return data.get("j") not in _store().value("auth", "revoked_sessions")


MAX_REVOKED = 5000  # entries last until their token would have expired anyway


def revoke_session(token: str | None) -> None:
    """Sign this one token out for good (until it would have expired anyway)."""
    data = _read_token(token)
    if data is None or not isinstance(data.get("j"), str):
        return
    now = int(time.time())
    with password_lock:
        revoked = {j: exp for j, exp in _store().value("auth", "revoked_sessions").items() if exp > now}
        revoked[data["j"]] = now + get_settings().session_days * 86400
        if len(revoked) > MAX_REVOKED:
            # A still-valid revocation must never be forgotten (that would bring a signed-out token
            # back to life): when the list cannot hold another one, end every session instead.
            end_all_sessions()
            return
        _store().update("auth", {"revoked_sessions": revoked})


def end_all_sessions() -> None:
    """Every session, on every device, is over."""
    with password_lock:
        _store().update("auth", {"session_epoch": session_epoch() + 1, "revoked_sessions": {}})


def trust_proxy() -> bool:
    """VIBEHEALTH_TRUST_PROXY: believe X-Forwarded-* headers (a proxy you run sits in front)."""
    return os.environ.get("VIBEHEALTH_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on")


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if trust_proxy():
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
    return False


def client_ip(request: Request) -> str:
    """Who is asking, as a throttle key (an IPv4 address or an IPv6 /64). Behind a proxy you run
    (VIBEHEALTH_TRUST_PROXY) it is the last hop the proxy wrote in X-Forwarded-For, when that is
    an IP address; otherwise, and always without the variable, the address of the connection."""
    if trust_proxy():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            hop = forwarded.split(",")[-1].strip()  # the hop our own proxy appended
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                pass  # not an address: ignore the header rather than let a client pick its own key
            else:
                return client_key(hop)
    return client_key(request.client.host if request.client else "unknown")


def set_session_cookie(request: Request, response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(),
        max_age=get_settings().session_days * 86400,
        httponly=True,
        samesite="strict",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=is_https(request)
    )


def throttled_attempt(request: Request, check: Callable[[], bool]) -> bool:
    """Run one password / code guess under the login throttle (see throttle.py): 429 (with
    Retry-After) while this client is locked or the whole app is backing off, else the result
    of `check`."""
    ip = client_ip(request)
    wait = login_throttle.reserve(ip)  # counted as a failure BEFORE checking: a burst cannot beat the budget
    if wait:
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts, try again later",
            headers={"Retry-After": str(wait)},
        )
    if check():
        login_throttle.success(ip)  # take the count back
        return True
    return False


OPEN_MODE_REFUSAL = (
    "This setting cannot be changed while no password is set: set a password first, "
    "or set it in the server environment."
)


def confirm_sensitive_change(request: Request, supplied: object) -> None:
    """Changes that decide where the server connects or whom it trusts (Paperless and Ollama
    addresses, trusted origins, allowed hosts) are refused while the app is open, and need the
    current password in the same request once one is set. Raises 403 (429 when throttled)."""
    stored = password_hash()
    if not stored:
        raise HTTPException(status_code=403, detail=OPEN_MODE_REFUSAL)

    def field(message: str) -> HTTPException:
        return HTTPException(status_code=403, detail=[{"field": "current_password", "message": message}])

    if not isinstance(supplied, str) or not supplied:
        raise field("Enter your current password to change this setting.")
    if len(supplied) > MAX_PASSWORD_LENGTH or not throttled_attempt(
        request, lambda: verify_password(supplied, stored)
    ):
        raise field("The current password is wrong.")


def require_session(vibehealth_session: str | None = Cookie(default=None)) -> None:
    """FastAPI dependency: 401 unless the request carries a valid session cookie."""
    if not password_hash():
        # No password: only reached when the install is open by VIBEHEALTH_LEGACY_OPEN or for the calls
        # that set the first password; every other call was already refused by the gate (setup_state.py).
        return
    if not valid_session(vibehealth_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")


# --- CSRF: Origin against Host -----------------------------------------------------------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_parts(origin: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
        if parts.scheme not in _DEFAULT_PORTS or not host:
            return None
        return parts.scheme, host, parts.port or _DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None


def origin_allowed(headers: Headers) -> bool:
    """A request without Origin comes from curl, a script or a test: allowed. A browser
    always sends one on a cross-site POST, and it has to name this host (or a trusted origin)."""
    origin = headers.get("origin")
    if origin is None:
        return True
    parsed = _origin_parts(origin)
    if parsed is None:  # "null" and anything else that is not an http(s) origin
        return False
    scheme, host, port = parsed

    request_host = headers.get("host", "")
    if trust_proxy() and headers.get("x-forwarded-host"):
        request_host = headers["x-forwarded-host"].split(",")[0].strip()
    try:
        own = urlsplit("//" + request_host)
        if own.hostname and own.hostname.lower() == host and (own.port or _DEFAULT_PORTS[scheme]) == port:
            return True
    except ValueError:
        pass

    for trusted in _store().value("general", "trusted_origins"):
        t = _origin_parts(trusted)
        if t and t == parsed:
            return True
    return False


class OriginCheckMiddleware:
    """Refuse state-changing requests that come from another site: by the Origin header
    (against the Host) and by the browser's own `Sec-Fetch-Site: cross-site` (403)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in _SAFE_METHODS:
            headers = Headers(scope=scope)
            if headers.get("sec-fetch-site", "").strip().lower() == "cross-site" or not origin_allowed(headers):
                response = JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --- Host allowlist: DNS-rebinding defence -----------------------------------------------------
#
# A page on evil.example can make a browser on your network talk to this app if evil.example's
# DNS name is switched to the app's address ("DNS rebinding"): the browser then treats the app as
# same-origin with the page, and the Origin check cannot tell. What still differs is the Host
# header, which carries the attacker's name. So only these Host values are answered:
# an IP address, `localhost`, a single-label name (`vibehealth`, a Docker service), a name ending
# in .local .lan .home.arpa .internal or Tailscale's .ts.net, and anything listed in
# VIBEHEALTH_ALLOWED_HOSTS (comma separated), in the setting general.allowed_hosts, or as the host
# of a trusted origin. Everything else gets 400.

_HOST_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".ts.net")
_HOST_RX = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::(\d{1,5}))?", re.I)


def split_host(value: str) -> str | None:
    """The lower-case host of a Host header value with its port removed, or None when the
    value is not a plain host[:port] (userinfo, paths, spaces and the like are refused)."""
    match = _HOST_RX.fullmatch(value.strip())
    if not match or (match.group(2) is not None and int(match.group(2)) > 65535):
        return None
    host = match.group(1).lower()
    if host.startswith("["):
        return host[1:-1]
    return host.rstrip(".") or None


def _normalise_entry(entry: str) -> str | None:
    """A host from a list entry that may be written as a URL or with a port."""
    entry = entry.strip()
    if "://" in entry:
        try:
            entry = urlsplit(entry).netloc
        except ValueError:
            return None
    return split_host(entry)


def env_allowed_hosts() -> set[str]:
    raw = os.environ.get("VIBEHEALTH_ALLOWED_HOSTS", "")
    return {h for h in (_normalise_entry(e) for e in raw.split(",") if e.strip()) if h}


def configured_hosts() -> set[str]:
    """Extra hosts: the environment, the saved setting, and the hosts of trusted origins."""
    hosts = env_allowed_hosts()
    try:
        store = _store()
        for name in store.value("general", "allowed_hosts"):
            if h := _normalise_entry(name):
                hosts.add(h)
        for origin in store.value("general", "trusted_origins"):
            if h := _normalise_entry(origin):
                hosts.add(h)
    except Exception:  # noqa: BLE001 - no database (yet): the built-in rules alone apply
        pass
    return hosts


def _builtin_host_ok(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host:  # single-label: a LAN or container name
        return True
    return host.endswith(_HOST_SUFFIXES)


def host_allowed(host_header: str | None) -> bool:
    host = split_host(host_header or "")
    if host is None:
        return False
    return _builtin_host_ok(host) or host in configured_hosts()


_last_host_warning = 0.0


class HostGuardMiddleware:
    """400 for a request whose Host header is not on the allowlist (all methods, all paths)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = Headers(scope=scope).get("host")
            if not host_allowed(host):
                self._warn(host)
                if scope["type"] == "http":
                    response = JSONResponse({"detail": "invalid host header"}, status_code=400)
                    await response(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _warn(host: str | None) -> None:
        global _last_host_warning
        now = time.monotonic()
        if now - _last_host_warning < 60:
            return
        _last_host_warning = now
        shown = re.sub(r"[^A-Za-z0-9.:\[\]_-]", "?", (host or "")[:80])
        log.warning(
            "refused a request with Host %r: to use another name, list it in "
            "VIBEHEALTH_ALLOWED_HOSTS (see docs/DESIGN.md)", shown,
        )


# --- startup warnings about exposure ---------------------------------------------------------------------


def bind_host(argv: list[str] | None = None) -> str | None:
    """The address the server was told to listen on (uvicorn --host, or VIBEHEALTH_HOST), if known."""
    args = list(sys.argv if argv is None else argv)
    for i, arg in enumerate(args):
        if arg == "--host" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return os.environ.get("VIBEHEALTH_HOST") or None


def _is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.0/8 and ::1
    except ValueError:
        return False


def reachable_beyond_loopback(host: str | None) -> bool:
    """Unknown counts as reachable (a container listens on 0.0.0.0)."""
    if host is None:
        return True
    return not _is_loopback(host)


def published_on_loopback() -> bool:
    """VIBEHEALTH_PUBLISHED_ON: the host address the container's port is published on (compose.yaml
    passes VIBEHEALTH_BIND, the Quadlet has it next to PublishPort). Listening on 0.0.0.0 inside the
    container is not reachable from the network when that address is a loopback one (127.0.0.0/8, ::1,
    localhost). Anything else, or nothing, is not: the warnings stay. Only silences warnings."""
    published = os.environ.get("VIBEHEALTH_PUBLISHED_ON", "").strip()
    return bool(published) and _is_loopback(published)


def log_exposure_warnings() -> list[str]:
    """At startup: say so when the app can be reached from the network without a password, or
    without HTTPS in front of it. Only logs; behaviour does not change. Returns what it logged."""
    host = bind_host()
    if not reachable_beyond_loopback(host) or published_on_loopback():
        return []
    where = f"listening on {host}" if host else "possibly reachable from other machines"
    messages = []
    if not password_hash():
        if legacy_open():
            messages.append(
                f"No password is set, VIBEHEALTH_LEGACY_OPEN is on and the app is {where}: anybody who can reach "
                "it can read the health records. Set a password in Settings (you need the setup code from this "
                "log or data/.setup-code) and remove VIBEHEALTH_LEGACY_OPEN."
            )
        else:
            messages.append(
                f"No password is set and the app is {where}: it refuses every request until one is set. "
                "Anybody who can reach it could claim it with the setup code, so keep that code private: "
                "set the password now (setup code in this log and data/.setup-code)."
            )
    if not trust_proxy():
        messages.append(
            f"The app is {where} over plain HTTP: the password and the session cookie cross the network "
            "unencrypted. Put a TLS reverse proxy in front of it and set VIBEHEALTH_TRUST_PROXY=1 "
            "(see docs/DESIGN.md, 'Exposing the app')."
        )
    for message in messages:
        log.warning(message)
    return messages


def reset_password() -> None:
    """Server-side recovery: forget the saved password so the app is open again, and make a
    new setup code (its location is printed, never its value). Every session ends."""
    from sqlmodel import Session

    from . import settings_store
    from .db import engine, init_db

    init_db()
    with Session(engine) as session:
        # the password, and any other unreadable auth row (that locks the app too, see password_hash)
        for name in ("password_hash", "revoked_sessions"):
            settings_store.drop(session, f"auth.{name}")
        session.commit()
    settings_store.clear_cache()
    settings_store.update("auth", {"session_epoch": session_epoch() + 1})
    if password_hash():
        clear_setup_code()
        print("The saved password was removed, but APP_PASSWORD_HASH is still set in the "
              "environment and applies. Remove it there too to open the app.")
        return
    new_setup_code(announce=False)
    print("The saved password was removed and every session ended.")
    print(f"Set a new password in Settings with the setup code saved in: {setup_code_path()}")
    print("(a running server also prints it in its log at the next start)")


def _main(argv: list[str]) -> None:
    import getpass

    if argv[:1] == ["reset-password"]:
        reset_password()
        return
    if argv:
        raise SystemExit("usage: python -m app.security [reset-password]")
    value = getpass.getpass("New app password: ")
    if value != getpass.getpass("Repeat: "):
        raise SystemExit("passwords do not match")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"use at least {MIN_PASSWORD_LENGTH} characters")
    print(f"\nAPP_PASSWORD_HASH={hash_password(value)}")


if __name__ == "__main__":
    import sys

    _main(sys.argv[1:])
