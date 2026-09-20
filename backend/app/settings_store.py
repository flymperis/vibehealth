"""Typed settings sections, kept in the `app_settings` key/value table.

Every setting is `<section>.<name>` (e.g. `paperless.url`) and resolves through
three layers, first hit wins:

    1. a value saved in the app (the database)          source "app"
    2. an environment variable (see config.py)          source "env"
    3. the built-in default                             source "default"

so the UI can say "set by environment". Secrets (marked per section) are
encrypted with secret_store, and are write-only through the API: the outside
world only ever sees `<name>_set`, `<name>_last4` and `<name>_source`.

Sections: paperless, general, uploads (shown by /api/settings); auth and setup
(internal); reading (see reading_settings.py, which keeps its own endpoints).

Values coming from the environment or the defaults are used as they are; values
coming from the database are validated, and one that no longer validates is
skipped (the layer below answers instead), never a crash.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session, select

from . import secret_store
from .config import get_settings
from .db import engine
from .models import AppSetting, DocumentKind

log = logging.getLogger("vibehealth")

APP, ENV, DEFAULT = "app", "env", "default"
_CACHE_TTL = 2.0  # seconds; changes made through this module invalidate at once


# --- section models -----------------------------------------------------------


_HOST_RX = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?)(:\d{1,5})?")
_HOSTNAME_RX = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
_URL_HINT = "must be an http(s) URL such as http://localhost:8000"


def clean_url(v: str) -> str:
    """A normalised http(s) base URL, or "" (not set). Raises ValueError with a reason.

    Kept strict because the app connects to it: a scheme and a host, nothing in
    front of the host (no user:password@), no query or fragment, and no
    whitespace, control or non-ASCII characters anywhere.
    """
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = v.strip()
    if not v:
        return ""
    if len(v) > 2048:
        raise ValueError("is too long")
    if not v.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in v):
        raise ValueError("must not contain spaces, control or non-ASCII characters")
    try:
        parts = urlsplit(v)
        port = parts.port
    except ValueError:
        raise ValueError(_URL_HINT) from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(_URL_HINT)
    if "@" in parts.netloc:
        raise ValueError("must not contain a user name or password")
    if not _HOST_RX.fullmatch(parts.netloc) or (port is not None and not 1 <= port <= 65535):
        raise ValueError("does not have a valid host name or port")
    if parts.query or parts.fragment or "?" in v or "#" in v:
        raise ValueError("must not contain a query string or fragment")
    return v.rstrip("/")


MATCH_MODES = ("type_or_tags", "type_only", "tags_only")
DEFAULT_TAGS = ["Blood Test", "Medical Report", "Prescription", "Imaging"]
# Paperless tag name -> what the document is. (Matched without regard to case.)
DEFAULT_KIND_MAP: dict[str, DocumentKind] = {
    "Blood Test": DocumentKind.BLOOD_TEST,
    "Medical Report": DocumentKind.REPORT,
    "Prescription": DocumentKind.PRESCRIPTION,
    "Imaging": DocumentKind.IMAGING,
}
MIN_SYNC_MINUTES = 5
MAX_SYNC_MINUTES = 10080  # a week


class PaperlessSection(BaseModel):
    enabled: bool = True  # off: nothing syncs, the saved connection is kept
    url: str = ""  # empty: Paperless is not connected
    public_url: str = ""  # what the browser opens for links; empty: use `url`
    token: str = ""  # secret
    document_type: str = Field("Medical", max_length=100)  # may be empty
    tags: list[str] = Field(default_factory=lambda: list(DEFAULT_TAGS), max_length=50)  # tag NAMES
    # Which documents count: the type, the tags, or either (the original behaviour).
    match: Literal["type_or_tags", "type_only", "tags_only"] = "type_or_tags"
    kind_map: dict[str, DocumentKind] = Field(default_factory=lambda: dict(DEFAULT_KIND_MAP), max_length=100)
    sync_interval_minutes: int = Field(240, ge=0, le=MAX_SYNC_MINUTES)  # 0: only "Sync now"

    model_config = {"extra": "forbid"}

    @field_validator("url", "public_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return clean_url(v)

    @field_validator("document_type")
    @classmethod
    def _document_type(cls, v: str) -> str:
        return v.strip()

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        v = [t.strip() for t in v if t.strip()]
        if any(len(t) > 100 for t in v):
            raise ValueError("a tag name is too long")
        return list(dict.fromkeys(v))

    @field_validator("kind_map")
    @classmethod
    def _kind_map(cls, v: dict[str, DocumentKind]) -> dict[str, DocumentKind]:
        out: dict[str, DocumentKind] = {}
        seen: set[str] = set()
        for name, kind in v.items():
            name = name.strip()
            if not name or len(name) > 100:
                raise ValueError("a tag name must be 1 to 100 characters")
            if name.lower() in seen:
                raise ValueError("the same tag name appears twice (case is ignored)")
            seen.add(name.lower())
            out[name] = kind
        return out

    @field_validator("sync_interval_minutes")
    @classmethod
    def _interval(cls, v: int) -> int:
        if 0 < v < MIN_SYNC_MINUTES:
            raise ValueError(f"use 0 (only when you press Sync now) or at least {MIN_SYNC_MINUTES} minutes")
        return v


def selection_problem(match: str, document_type: str, tags: list[str]) -> str | None:
    """Why this choice of documents would match nothing, or None when it is a real choice."""
    has_type, has_tags = bool(document_type.strip()), any(t.strip() for t in tags)
    if match == "type_only" and not has_type:
        return "Enter a document type, or change what counts as a medical document."
    if match == "tags_only" and not has_tags:
        return "Choose at least one tag, or change what counts as a medical document."
    if match == "type_or_tags" and not (has_type or has_tags):
        return "Choose a document type or at least one tag."
    return None


def _check_paperless(before: dict, after: dict, supplied: set[str]) -> list[dict]:
    errors: list[dict] = []
    changed = (after["url"] or "").rstrip("/") != (before["url"] or "").rstrip("/")
    if changed and after["token"] and "token" not in supplied:
        errors.append({
            "field": "token",
            "message": "The URL changed, so enter the API token again: a saved token is only "
                       "ever sent to the address it was saved for.",
        })
    if after["enabled"]:
        problem = selection_problem(after["match"], after["document_type"], after["tags"])
        if problem:
            errors.append({"field": "selection", "message": problem})
    return errors


class GeneralSection(BaseModel):
    language: Literal["en", "el"] = "en"
    # Origins allowed to send state-changing requests, besides the Host itself.
    trusted_origins: list[str] = Field(default_factory=list, max_length=20)
    # Host names the app answers to, besides IP addresses, localhost, single-label names and
    # .local / .lan / .home.arpa / .internal / .ts.net (see security.HostGuardMiddleware).
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)

    model_config = {"extra": "forbid"}

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        out = []
        for host in v:
            host = host.strip().lower().rstrip(".")
            if len(host) > 253 or not _HOSTNAME_RX.fullmatch(host):
                raise ValueError("each entry must be a host name such as health.example.com (no port, no scheme)")
            out.append(host)
        return list(dict.fromkeys(out))

    @field_validator("trusted_origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        out = []
        for origin in v:
            origin = origin.strip().rstrip("/")
            if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]]+(:\d{1,5})?", origin):
                raise ValueError("each origin must look like https://host[:port]")
            out.append(origin)
        return list(dict.fromkeys(out))


MAX_UPLOAD_MB = 200  # an upper bound: a phone photo or a scanned report is a few MB
MIN_TOTAL_UPLOAD_MB = 100
MAX_TOTAL_UPLOAD_MB = 1_000_000  # 1 TB


class UploadsSection(BaseModel):
    enabled: bool = True
    max_file_mb: int = Field(50, ge=1, le=MAX_UPLOAD_MB)
    # The most that uploads/ may hold in all (originals and files still arriving): an upload that
    # would go over it is refused with 507. Default 10 GB.
    max_total_mb: int = Field(10240, ge=MIN_TOTAL_UPLOAD_MB, le=MAX_TOTAL_UPLOAD_MB)

    model_config = {"extra": "forbid"}


class AuthSection(BaseModel):
    password_hash: str = ""  # internal: never returned by any endpoint
    session_epoch: int = Field(0, ge=0)  # bumped on a password change: ends every session
    revoked_sessions: dict[str, int] = Field(default_factory=dict, max_length=6000)  # token id -> expiry (security.MAX_REVOKED is below it)

    model_config = {"extra": "forbid"}


class SetupSection(BaseModel):
    completed: bool = False  # the first-run guide was finished (see setup_state.py: the state is derived)

    model_config = {"extra": "forbid"}


# --- section registry ---------------------------------------------------------


@dataclass(frozen=True)
class Section:
    name: str
    model: type[BaseModel]
    env: dict[str, str] = field(default_factory=dict)  # setting -> attribute of config.Settings
    secrets: frozenset[str] = frozenset()
    api: bool = False  # listed and editable through /api/settings
    # Rules that span several settings, run on the values as they would be after an
    # update: check(before, after, names_of_secrets_supplied) -> [{field, message}]
    check: Callable[[dict, dict, set[str]], list[dict]] | None = None

    @property
    def names(self) -> list[str]:
        return list(self.model.model_fields)


PAPERLESS = Section(
    "paperless",
    PaperlessSection,
    env={
        "url": "paperless_url",
        "public_url": "paperless_public_url",
        "token": "paperless_token",
        "document_type": "paperless_document_type",
        "tags": "paperless_tags",
        "sync_interval_minutes": "sync_interval_minutes",
    },
    secrets=frozenset({"token"}),
    api=True,
    check=_check_paperless,
)
GENERAL = Section(
    "general",
    GeneralSection,
    env={"language": "default_language", "trusted_origins": "trusted_origins"},
    api=True,
)
UPLOADS = Section("uploads", UploadsSection, api=True)
AUTH = Section("auth", AuthSection, env={"password_hash": "app_password_hash"})
SETUP = Section("setup", SetupSection)

SECTIONS: dict[str, Section] = {s.name: s for s in (PAPERLESS, GENERAL, UPLOADS, AUTH, SETUP)}


# Settings that decide where the server connects or whom it trusts. Changing one is refused
# while no password is set (the environment still works) and needs the current password
# once one is set, so that a visitor to an open instance, or a forged request, cannot point
# the server (and the API token it sends) somewhere else.
SENSITIVE: dict[str, frozenset[str]] = {
    "paperless": frozenset({"url", "public_url"}),
    "general": frozenset({"trusted_origins", "allowed_hosts"}),
    "reading": frozenset({"ollama_url"}),
}


def sensitive_changes(section: Section | str, changes: dict[str, Any]) -> list[str]:
    """Names in `changes` that are sensitive and would change the effective value.
    A value equal to the one in force (a form sent whole) is not a change."""
    if isinstance(section, str):
        section = SECTIONS[section]
    names = [n for n in changes if n in SENSITIVE.get(section.name, frozenset())]
    if not names:
        return []
    current = resolve(section, use_cache=False).values
    changed = []
    for name in names:
        val = changes[name]
        try:
            new = _lower_layer(section, name)[0] if val is None else getattr(section.model(**{name: val}), name)
        except (ValidationError, ValueError, TypeError):
            new = object()  # unusable input: not equal to anything, so it counts (and is then rejected)
        if new != current[name]:
            changed.append(name)
    return changed


def register(section: Section) -> None:
    """Add a section defined elsewhere (reading_settings does)."""
    SECTIONS[section.name] = section


# --- rows ---------------------------------------------------------------------


def rows(session: Session, prefix: str) -> dict[str, str]:
    return {r.key: r.value for r in session.exec(select(AppSetting).where(AppSetting.key.startswith(prefix)))}


def put(session: Session, key: str, raw: str) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key)
    row.value = raw
    row.updated_at = datetime.now()
    session.add(row)


def drop(session: Session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        session.delete(row)


# --- resolving ----------------------------------------------------------------


@dataclass
class Resolved:
    values: dict[str, Any]  # secrets in plaintext: never hand this to a response
    sources: dict[str, str]
    # settings that ARE saved in the database but could not be read (so a lower layer answered)
    broken: frozenset[str] = frozenset()


_cache: dict[str, tuple[float, Resolved]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _lower_layer(section: Section, name: str) -> tuple[Any, str]:
    attr = section.env.get(name)
    if attr is not None:
        s = get_settings()
        val = getattr(s, attr)
        # A variable that is present but empty (VAR=) counts as not set.
        return val, (ENV if attr in s.model_fields_set and val not in ("", []) else DEFAULT)
    return section.model.model_fields[name].get_default(call_default_factory=True), DEFAULT


def _from_db(section: Section, name: str, raw: str) -> tuple[bool, Any]:
    if name in section.secrets:
        plain = secret_store.decrypt(raw)
        return (bool(plain), plain)
    try:
        value = json.loads(raw)
        return True, getattr(section.model(**{name: value}), name)
    except (ValueError, ValidationError):
        return False, None


def resolve(section: Section | str, *, use_cache: bool = True) -> Resolved:
    """The effective value of every setting in a section, and where each comes from."""
    if isinstance(section, str):
        section = SECTIONS[section]
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(section.name)
            if hit and now - hit[0] < _CACHE_TTL:
                return Resolved(dict(hit[1].values), dict(hit[1].sources), hit[1].broken)
    prefix = section.name + "."
    with Session(engine) as session:
        stored = rows(session, prefix)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    broken: set[str] = set()
    for name in section.names:
        raw = stored.get(prefix + name)
        if raw is not None:
            ok, value = _from_db(section, name, raw)
            if ok:
                values[name], sources[name] = value, APP
                continue
            broken.add(name)
        values[name], sources[name] = _lower_layer(section, name)
    result = Resolved(values, sources, frozenset(broken))
    if use_cache:
        with _cache_lock:
            _cache[section.name] = (now, Resolved(dict(values), dict(sources), frozenset(broken)))
    return result


def value(section: str, name: str) -> Any:
    return resolve(section).values[name]


# --- writing ------------------------------------------------------------------


class SettingsError(ValueError):
    """Rejected input. `errors` are [{field, message}] and never contain the input."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("invalid settings")
        self.errors = errors


def clean_secret(v: Any) -> str:
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = v.strip()
    if not v:
        raise ValueError("must not be blank (send an empty string to clear it)")
    if len(v) > 1024:
        raise ValueError("is too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("contains invalid characters")
    return v


def update(section: Section | str, changes: dict[str, Any], *, dry_run: bool = False) -> None:
    """Apply a partial update, all or nothing. With `dry_run` only check that each value is
    usable on its own (SettingsError); the rules that span several settings are left to the real call.

    Plain settings: a value sets it, `None` removes the saved value (the
    environment or default answers again). Secrets: a string sets it, `""`
    clears it. Anything not mentioned stays as it is.
    """
    if isinstance(section, str):
        section = SECTIONS[section]
    errors: list[dict] = []
    plain: dict[str, Any] = {}
    clears: list[str] = []
    secret_set: dict[str, str] = {}
    secret_clear: list[str] = []
    known = set(section.names)
    for name, val in changes.items():
        if name not in known:
            errors.append({"field": str(name), "message": "unknown setting"})
        elif name in section.secrets:
            if val == "":
                secret_clear.append(name)
            else:
                try:
                    secret_set[name] = clean_secret(val)
                except ValueError as exc:
                    errors.append({"field": name, "message": str(exc)})
        elif val is None:
            clears.append(name)
        else:
            plain[name] = val
    validated: BaseModel | None = None
    if plain:
        try:
            validated = section.model(**plain)
        except ValidationError as exc:
            for e in exc.errors():  # loc and msg only: `input` could be a secret
                # a dict key in the location is user input too: keep the field and list indexes only
                loc = [e["loc"][0]] + [p for p in e["loc"][1:] if isinstance(p, int)]
                errors.append({
                    "field": ".".join(str(p) for p in loc),
                    "message": e["msg"].removeprefix("Value error, "),
                })
    if not errors and section.check is not None and not dry_run:  # a dry run checks each value only
        before = resolve(section, use_cache=False).values
        after = dict(before)
        for name in plain:
            after[name] = getattr(validated, name)
        for name in clears + secret_clear:
            after[name] = _lower_layer(section, name)[0]
        after.update(secret_set)
        errors.extend(section.check(before, after, set(secret_set)))
    if errors:
        raise SettingsError(errors)
    if dry_run:
        return

    with Session(engine) as session:
        for name in plain:
            put(session, f"{section.name}.{name}", json.dumps(getattr(validated, name)))
        for name in clears + secret_clear:
            drop(session, f"{section.name}.{name}")
        for name, plaintext in secret_set.items():
            put(session, f"{section.name}.{name}", secret_store.encrypt(plaintext))
        session.commit()
    clear_cache()


def set_value(section: str, name: str, val: Any) -> None:
    update(section, {name: val})


# --- what the API may show ------------------------------------------------------


def public_view(section: Section | str) -> dict:
    """Values and sources of a section, with secrets reduced to set / last4 / source."""
    if isinstance(section, str):
        section = SECTIONS[section]
    r = resolve(section, use_cache=False)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in section.names:
        if name in section.secrets:
            plain = r.values[name]
            is_set = bool(plain)
            values[f"{name}_set"] = is_set
            values[f"{name}_last4"] = secret_store.last4(plain) if is_set else ""
            values[f"{name}_source"] = r.sources[name] if is_set else DEFAULT
        else:
            values[name] = r.values[name]
            sources[name] = r.sources[name]
    return {"values": values, "sources": sources}


def log_sources() -> None:
    """At startup: where each setting comes from, by name only, never the value."""
    for section in SECTIONS.values():
        r = resolve(section, use_cache=False)
        log.info(
            "settings %s: %s",
            section.name,
            " ".join(f"{n}={r.sources[n]}" for n in section.names),
        )
