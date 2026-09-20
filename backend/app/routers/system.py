"""Sync trigger and service status."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from .. import settings_store
from .. import uploads as uploads_module
from ..db import get_session
from ..models import Document
from ..paperless import Paperless
from ..security import password_hash, require_session
from ..worker import request_sync, state, sync_plan

router = APIRouter(prefix="/api", tags=["system"], dependencies=[Depends(require_session)])


@router.post("/sync")
def sync_now() -> dict:
    """Ask for a sync. Nothing is queued while Paperless is off or not connected."""
    plan = sync_plan()
    if not plan.active:
        return {"ok": True, "queued": False, "reason": plan.skipped}
    request_sync()
    return {"ok": True, "queued": True, "reason": None}


@router.get("/status")
def status(session: Session = Depends(get_session)) -> dict:
    total = session.exec(select(func.count()).select_from(Document)).one()
    ignored = session.exec(
        select(func.count()).select_from(Document).where(Document.ignored == True)  # noqa: E712
    ).one()
    plan = sync_plan()
    uploads = settings_store.resolve("uploads", use_cache=False).values
    return {
        "documents": {"total": total, "ignored": ignored},
        "worker": state,
        "paperless_url": Paperless().url,
        # Why the list may be empty: Paperless off, or no address/token yet.
        "paperless": {
            "enabled": plan.enabled,
            "configured": plan.configured,
            "sync_active": plan.active,
            "skipped": plan.skipped,  # null | "disabled" | "not_configured"
            "sync_interval_minutes": plan.minutes,  # 0: only "Sync now"
        },
        # Why uploading may be unavailable: turned off (enabled false), or no password set yet
        # (password_required true: an upload is refused while the app is open to everybody).
        "uploads": {
            "enabled": bool(uploads["enabled"]),
            "max_mb": int(uploads["max_file_mb"]),
            "password_required": not password_hash(),
            # what the uploads folder holds (MB, one decimal) and the most it may hold (setting
            # uploads.max_total_mb): at the limit, an upload answers 507
            "total_mb_used": round(uploads_module.total_bytes() / (1024 * 1024), 1),
            "max_total_mb": int(uploads["max_total_mb"]),
        },
    }
