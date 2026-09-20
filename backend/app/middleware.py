"""Response headers on everything, and a cap on request body size.

Order in main.py (outermost first): SecurityHeaders, HostGuard, OriginCheck, BodyLimit, so that
even a refusal made by an outer layer carries the headers.
"""

from __future__ import annotations

from collections.abc import Callable

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --- security headers ----------------------------------------------------------------------------

# The React app is one script and one stylesheet from this origin, inline `style` attributes,
# images from here / data: / blob:, and calls to /api on the same origin. Nothing else is allowed.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}


class SecurityHeadersMiddleware:
    """Add the headers above to every response. Under /api/ a response that does not say
    otherwise (thumbnails and previews do: `private`) is `private, no-store`: it holds medical data."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if is_api and "cache-control" not in headers:
                    headers["Cache-Control"] = "private, no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --- request body size -------------------------------------------------------------------------------

DEFAULT_MAX_BODY = 1024 * 1024  # 1 MiB: every JSON route of the app
_route_limits: dict[str, int | Callable[[], int]] = {}


def set_body_limit(path_prefix: str, max_bytes: int | Callable[[], int]) -> None:
    """Allow a larger body for one route (the document upload): every path that starts with
    `path_prefix` gets `max_bytes`. The longest matching prefix wins; the rest keep the default.
    A function is asked on every request, for a limit that can be changed in Settings."""
    _route_limits[path_prefix] = max_bytes


def body_limit_for(path: str) -> int:
    matches = [p for p in _route_limits if path.startswith(p)]
    if not matches:
        return DEFAULT_MAX_BODY
    limit = _route_limits[max(matches, key=len)]
    return int(limit() if callable(limit) else limit)


class _BodyTooLarge(HTTPException):
    """An HTTPException, because FastAPI turns any other error raised while it reads the body
    into a 400, but lets an HTTPException through to answer with its own status."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class BodyLimitMiddleware:
    """413 for a request body over the limit for its path: by Content-Length when it is sent,
    and by counting the bytes as they arrive when it is not (chunked) or is not true."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                size = -1
            if size < 0:
                await JSONResponse({"detail": "bad content length"}, status_code=400)(scope, receive, send)
                return
            if size > limit:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if started:  # too late to change the answer: drop the connection's remaining work
                return
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
