"""First-run setup: what state the install is in, and finishing the guide.

The state is derived (see setup_state.py). `status` is public: the wizard and the login page need it
before there is a session, so it holds only what they need: four booleans/words, nothing about
Paperless, uploads, Ollama or whether documents exist.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import settings_store, setup_state
from ..security import password_hash, require_session

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def setup_status() -> dict:
    has_password = bool(password_hash())
    return {
        "state": setup_state.state(),  # "needs_setup" | "ready"
        "password_set": has_password,
        "needs_setup_code": not has_password,  # the first password needs the code from the server
        "wizard_pending": setup_state.wizard_pending(),
    }


@router.post("/complete", dependencies=[Depends(require_session)])
def complete_setup() -> dict:
    """The guide is finished. Needs a password: a fresh install is never left open."""
    if not password_hash():
        raise HTTPException(409, "set a password first")
    settings_store.update("setup", {"completed": True})
    return {"ok": True}
