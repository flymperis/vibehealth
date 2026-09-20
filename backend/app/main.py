"""VibeHealth API + the built frontend, served from one container."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import hardening, sandbox, secret_store, security, settings_store, uploads
from .config import get_settings
from .db import init_db
from .migrations import MigrationError
from .middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from .routers import auth, documents, reading, system
from .routers import paperless as paperless_router
from .routers import settings as settings_router
from .routers import setup as setup_router
from .security import HostGuardMiddleware, OriginCheckMiddleware
from .setup_state import SetupGateMiddleware
from .worker import loop, reading_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
secret_store.install_log_redaction()
log = logging.getLogger("vibehealth")

FRONTEND_DIR = os.environ.get("VIBEHEALTH_FRONTEND", "/app/static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    hardening.apply_umask()  # no file below is made readable by another user
    hardening.check_single_worker()  # the queue, the throttle and the limits live in this one process
    secret_store.register_env_secrets()  # masked in every log line from here on
    try:
        init_db()  # migrations first: nothing reads the database before this
    except MigrationError as exc:
        log.critical("%s", exc)  # one clear line (it names the backup), then the app does not start
        raise
    try:
        uploads.sweep_at_start()  # partly received files, and deletes that could not finish (never raises)
    except Exception:  # noqa: BLE001 - a failing clean-up must never stop the app from starting
        log.warning("the start-up clean-up of uploads failed", exc_info=True)
    secret_store.ensure_key()
    settings_store.log_sources()  # names and sources only, never values
    security.ensure_setup_code()  # no password yet: a new code in the log and data/.setup-code
    security.log_exposure_warnings()
    try:  # can the sandbox child work under its limits? (steps the memory limit down if not; never raises)
        await asyncio.to_thread(sandbox.selftest)
    except Exception:  # noqa: BLE001
        log.warning("the sandbox self-test could not run", exc_info=True)
    hardening.secure_data_dir()  # 0700 / 0600 on everything that exists now (POSIX)
    tasks = [
        asyncio.create_task(loop(), name="vibehealth-sync"),
        asyncio.create_task(reading_loop(), name="vibehealth-reading"),
        asyncio.create_task(uploads.sweep_loop(), name="vibehealth-upload-sweep"),  # .tmp every 10 min
    ]
    log.info("VibeHealth started (data at %s)", get_settings().data_dir)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def dev_docs() -> bool:
    """VIBEHEALTH_DEV_DOCS=1 turns on /docs, /redoc and /openapi.json (development only:
    they describe every endpoint to anybody who can reach the app)."""
    return os.environ.get("VIBEHEALTH_DEV_DOCS", "").strip() == "1"


_DOCS = {} if dev_docs() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="VibeHealth", lifespan=lifespan, **_DOCS)
# The last one added is the outermost: headers, then Host, then Origin, then the body cap,
# and innermost the gate for an install without a password (403 setup_required, see setup_state.py).
app.add_middleware(SetupGateMiddleware)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(HostGuardMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(documents.values_router)
app.include_router(documents.dashboard_router)
app.include_router(documents.examinations_router)
app.include_router(reading.router)
app.include_router(system.router)
app.include_router(settings_router.router)
app.include_router(paperless_router.router)
app.include_router(setup_router.router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Like FastAPI's own 422, minus `input` and `ctx`: a rejected password or token must not be echoed."""
    detail = [{"type": e["type"], "loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.api_route(
    "/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
)
def api_not_found(rest: str) -> None:
    """Any method on an unknown API path is a 404 (not the SPA's 405)."""
    raise HTTPException(404, "not found")


class _Assets(StaticFiles):
    def lookup_path(self, path: str):
        if "\0" in path:  # os.stat raises ValueError on a NUL byte: a missing file, not a 500
            return "", None
        return super().lookup_path(path)


if os.path.isdir(FRONTEND_DIR):
    app.mount("/assets", _Assets(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        """Everything that is not /api is the React app (client-side routing).

        index.html names the hashed asset files, so a browser holding on to an
        old copy of it keeps running an old build after every deploy. Assets are
        safe to cache forever because their names change; this page is not.
        """
        # An unknown API path is a missing endpoint, not a page: answering it
        # with index.html and a 200 hides the mistake from whatever called it.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "not found")
        # With the API docs off, their paths are missing pages too, not the app shell.
        if not dev_docs() and full_path.rstrip("/") in ("docs", "redoc", "openapi.json", "docs/oauth2-redirect"):
            raise HTTPException(404, "not found")
        if "\0" in full_path:  # realpath raises ValueError on a NUL byte: a missing page, not a 500
            raise HTTPException(404, "not found")
        # Only files that really live inside the frontend directory are served.
        # `join` throws the base away for an absolute path ("//etc/passwd") and
        # an encoded "..%2f" is decoded before it gets here, so resolve the path
        # (symlinks included) and require it to stay under the root.
        root = os.path.realpath(FRONTEND_DIR)
        candidate = os.path.realpath(os.path.join(root, full_path))
        if full_path and candidate.startswith(root + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(
            os.path.join(FRONTEND_DIR, "index.html"),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
