"""Reading settings, the Ollama connection check, queue status and the test catalogue."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError

from .. import model_check, reading_settings, settings_store, uploads
from ..catalog import TESTS
from ..middleware import set_body_limit
from ..ollama import Ollama, OllamaError, describe, detect, has_model
from ..reading_settings import ReadingSettings
from ..security import confirm_sensitive_change, password_hash, require_session
from ..throttle import detect_limiter, test_limiter
from ..worker import check_state, state

router = APIRouter(prefix="/api/reading", tags=["reading"], dependencies=[Depends(require_session)])


@router.get("/settings")
def get_reading_settings() -> dict:
    return {
        "settings": reading_settings.load(),
        "defaults": reading_settings.defaults(),
        "sources": reading_settings.sources(),
    }


@router.put("/settings")
def put_reading_settings(body: dict, request: Request) -> dict:
    body = dict(body)
    confirmation = body.pop("current_password", None)  # a request field, never a setting
    try:
        new = ReadingSettings(**body)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors()
        ]
        raise HTTPException(422, errors) from exc
    if new.ollama_url != reading_settings.load().ollama_url:
        # the server sends page images to this address: same rule as the Paperless address
        confirm_sensitive_change(request, confirmation)
    return {
        "settings": reading_settings.save(new),
        "defaults": reading_settings.defaults(),
        "sources": reading_settings.sources(),
    }


class ConnectionBody(BaseModel):
    ollama_url: str | None = None
    current_password: str | None = None  # needed to try an address other than the saved one


@router.post("/test-connection")
async def test_connection(request: Request, body: ConnectionBody | None = None) -> dict:
    """List the installed models at the saved address. With a password set, `ollama_url`
    may name another address to try before saving it, together with `current_password` (as saving it
    needs); while the app is open only the saved one can be tried (403), so the server cannot be pointed
    at other machines by a visitor. Answers are fixed messages, never the text of an exception."""
    wait = test_limiter.hit()
    if wait:
        raise HTTPException(429, f"Too many requests. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    current = reading_settings.load()
    url = current.ollama_url
    if body and body.ollama_url:
        try:
            url = ReadingSettings(ollama_url=body.ollama_url).ollama_url
        except ValidationError as exc:
            return {"ok": False, "url": "", "models": [],
                    "error": exc.errors()[0]["msg"].removeprefix("Value error, ")}
        if url != current.ollama_url:
            confirm_sensitive_change(request, body.current_password)  # 403 while open, the password once set
    try:
        models = await Ollama(url, 10, current.num_ctx, current.keep_alive).models()
    except OllamaError as exc:
        return {"ok": False, "url": url, "models": [], "error": describe(exc)}
    return {"ok": True, "url": url, "models": models, "error": ""}


@router.get("/status")
def reading_status() -> dict:
    return state["reading"]


@router.get("/catalog")
def catalog() -> list[dict]:
    return [
        {"code": t.code, "name_en": t.name_en, "name_el": t.name_el,
         "specimen": t.specimen, "category": t.category, "unit": t.unit}
        for t in TESTS
    ]


@router.get("/readiness")
async def readiness() -> dict:
    """Is Ollama reachable and are the models the reader settings name installed? Answers are
    fixed messages and commands built from validated model names, never exception text."""
    current = reading_settings.load()
    ollama = Ollama(current.ollama_url, 10, current.num_ctx, current.keep_alive)
    reachable, version, installed = True, "", []
    try:
        installed = await ollama.models()
    except OllamaError:
        reachable = False
    if reachable:
        version = await ollama.version()
    wanted = [("reader_a", current.reader_a_model)]
    if current.reader_b_enabled:  # reader B is optional: not required while it is off
        wanted.append(("reader_b", current.reader_b_model))
    models = [
        {"role": role, "name": name, "installed": reachable and has_model(installed, name),
         "pull_command": f"ollama pull {name}"}
        for role, name in wanted
    ]
    missing = [m["pull_command"] for m in models if not m["installed"]]
    return {
        "ollama": {"reachable": reachable, "version": version, "url": current.ollama_url},
        "models": models,
        "ready": reachable and not missing,
        "missing": missing,
    }


@router.get("/models")
async def reading_models() -> dict:
    """The installed models, each with what the project knows about it (see model_check.KNOWN) and whether
    Ollama says it can see images, plus the counts of the last model checks. The address is the saved one."""
    current = reading_settings.load()
    ollama = Ollama(current.ollama_url, 10, current.num_ctx, current.keep_alive)
    checks = await asyncio.to_thread(model_check.history)
    try:
        names = await ollama.models()
    except OllamaError as exc:
        return {"ok": False, "error": describe(exc), "models": [], "checks": checks}
    capabilities = await asyncio.gather(*(ollama.capabilities(n) for n in names))
    return {"ok": True, "error": "", "checks": checks,
            "models": [model_check.model_info(n, c) for n, c in zip(names, capabilities)]}


CHECK_PATH = "/api/reading/model-check"
# The same size limit as an upload (Settings > Uploads > largest file), plus the multipart wrapping.
set_body_limit(
    CHECK_PATH,
    lambda: uploads.cap_bytes(settings_store.value("uploads", "max_file_mb")) + uploads.MULTIPART_OVERHEAD,
)
_CHECK_BODY = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["file", "model"],
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "model": {"type": "string"},
                "use_reader_b": {"type": "boolean", "default": False},
                "expected": {"type": "string"},
            },
        }}},
    }
}


@router.get("/model-check")
def model_check_status() -> dict:
    """Progress of the running check (null when none) and the result of the last one (memory only)."""
    return dict(check_state)


@router.post("/model-check", status_code=202, openapi_extra=_CHECK_BODY)
async def start_model_check(request: Request) -> dict:
    """Try an installed model on a sample file: multipart/form-data with `file` (PDF, JPEG, PNG or WebP, the same
    limits as an upload), `model` (one Ollama lists), optional `use_reader_b` and `expected` (one known value
    per line). 202 when it has started: it runs in the background (GET /model-check for progress and result).
    Nothing is saved but counts. 403 while uploads are turned off or no password is set (it accepts a file), 409 while a document is being
    read or another check is running."""
    if not settings_store.resolve("uploads", use_cache=False).values["enabled"]:
        raise HTTPException(403, "Uploads are turned off in Settings.")  # a check takes a file, like an upload
    if not password_hash():
        raise HTTPException(
            403, "Set a password first: a model check takes a file, and that is only allowed once the app is "
                 "protected by a password."
        )
    try:
        progress = model_check.begin()
    except model_check.CheckBusy as exc:
        raise HTTPException(409, str(exc)) from None
    received: uploads.Received | None = None
    handed_over = False
    try:
        try:
            others = uploads.begin_upload()
        except uploads.UploadError as exc:
            raise HTTPException(exc.status, exc.message, headers=exc.headers) from None
        try:
            cfg = settings_store.resolve("uploads", use_cache=False).values
            cap = uploads.cap_bytes(cfg["max_file_mb"])
            max_total = int(cfg["max_total_mb"]) * 1024 * 1024
            try:
                declared = int(request.headers.get("content-length", ""))
            except ValueError:
                declared = None
            used = await asyncio.to_thread(uploads.check_room, cap, max_total, others, declared if declared else None)
            received = await uploads.receive(request, cap, max(0, max_total - used), model_check.TEXT_FIELDS)
            plan = await model_check.prepare(received)
        finally:
            uploads.end_upload()
        model_check.start(received.path, plan, progress)  # from here the task owns the file and the place
        handed_over = True
    except uploads.UploadError as exc:
        raise HTTPException(exc.status, exc.message, headers=exc.headers) from None
    finally:
        if not handed_over:
            if received is not None:
                uploads.remove_quietly(received.path)
            model_check.release()
    return {"started": True}


@router.post("/detect-ollama")
async def detect_ollama() -> dict:
    """Which of a fixed list of usual addresses answers like Ollama. The request carries nothing:
    the server never probes an address a client names."""
    wait = detect_limiter.hit()
    if wait:
        raise HTTPException(429, f"Too many requests. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    return {"found": await detect()}
