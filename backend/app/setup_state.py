"""Has this install been set up? Derived at runtime, nothing to migrate.

An install is READY only when

  - a password exists (saved in the app or APP_PASSWORD_HASH; a saved hash that cannot be read
    counts: the app is locked, not open), or
  - the operator explicitly set `VIBEHEALTH_LEGACY_OPEN=1` ("keeps an installation without a password
    open; not recommended").

Nothing is inferred from a configured Paperless token, from documents or from any stored flag.
Otherwise the install is `needs_setup`: the API answers 403 `{"detail": "setup_required"}` for
everything except the few calls the wizard needs to set the first password (which also needs the
setup code). The same holds after `python -m app.security reset-password`. The moment a password
exists the state is `ready` and the session works normally; the wizard carries on with the rest.
"""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import settings_store
from .security import legacy_open, password_hash

NEEDS_SETUP, READY = "needs_setup", "ready"

# Open while the app is a fresh install: health, the setup endpoints, the auth status (the login
# page reads it) and the one call that sets the first password (it checks the setup code itself).
_OPEN_EXACT = {("GET", "/api/health"), ("GET", "/api/auth/status"), ("POST", "/api/auth/change-password")}
_OPEN_PREFIX = "/api/setup/"


def setup_completed() -> bool:
    return bool(settings_store.value("setup", "completed"))


def wizard_pending() -> bool:
    """Should the guide open by itself? Yes while the install needs its first password, and after a
    password exists until the guide was finished or skipped once. Never in explicit legacy-open mode
    (no password, operator's choice). Derived from the password and one flag only: it is public, so it
    must not say anything about documents or Paperless."""
    if password_hash():
        return not setup_completed()
    return not legacy_open()


def state() -> str:
    """READY with a password or `VIBEHEALTH_LEGACY_OPEN=1`; otherwise `needs_setup`."""
    return READY if (password_hash() or legacy_open()) else NEEDS_SETUP


def gated(method: str, path: str) -> bool:
    """Is this request one the fresh-install gate covers?"""
    if not (path == "/api" or path.startswith("/api/")):
        return False  # the app shell and its assets
    return not ((method, path) in _OPEN_EXACT or path.startswith(_OPEN_PREFIX))


class SetupGateMiddleware:
    """403 `setup_required` for the API while the install is fresh (see the module text)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and gated(scope["method"], scope["path"]):
            if await asyncio.to_thread(state) == NEEDS_SETUP:
                response = JSONResponse({"detail": "setup_required"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
