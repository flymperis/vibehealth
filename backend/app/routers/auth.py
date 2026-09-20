"""Login with the single app password, and changing it."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .. import settings_store
from ..security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    check_setup_code,
    clear_session_cookie,
    end_all_sessions,
    legacy_open,
    maybe_rehash,
    password_hash,
    password_lock,
    require_session,
    revoke_session,
    set_password,
    set_session_cookie,
    throttled_attempt,
    valid_session,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordBody(BaseModel):
    current: str = Field("", max_length=MAX_PASSWORD_LENGTH)
    new: str = Field(max_length=MAX_PASSWORD_LENGTH)
    setup_code: str = Field("", max_length=MAX_PASSWORD_LENGTH)  # only for the first password (see security.py)

    @field_validator("new")
    @classmethod
    def _new(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"use at least {MIN_PASSWORD_LENGTH} characters")
        if len(v) > MAX_PASSWORD_LENGTH:
            raise ValueError(f"use at most {MAX_PASSWORD_LENGTH} characters")
        return v


@router.get("/status")
def status(vibehealth_session: str | None = Cookie(default=None)) -> dict:
    has_password = bool(password_hash())
    language = settings_store.value("general", "language")
    if has_password:
        mode = "password"
    elif legacy_open():
        mode = "open_legacy"  # VIBEHEALTH_LEGACY_OPEN=1 and no password: everything open, not recommended
    else:
        mode = "needs_setup"  # nothing works until the first password is set
    return {
        "password_required": has_password,
        "authenticated": valid_session(vibehealth_session) if has_password else mode == "open_legacy",
        "default_language": language,
        "language": language,
        "password_set": has_password,
        "setup_code_required": not has_password,  # the first password needs the code from the server
        "mode": mode,
    }


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    stored = password_hash()
    if not stored:
        return {"ok": True, "note": "no password configured"}
    if not throttled_attempt(request, lambda: verify_password(body.password, stored)):
        raise HTTPException(status_code=401, detail="wrong password")
    maybe_rehash(body.password, stored)  # a hash from older settings is replaced now that we have the password
    set_session_cookie(request, response)
    return {"ok": True}


@router.post("/change-password", dependencies=[Depends(require_session)])
def change_password(body: ChangePasswordBody, request: Request, response: Response) -> dict:
    """Set a new password; every other session ends. With no password yet the first one is
    set with the one-time setup code (server log / data/.setup-code) instead of `current`."""
    with password_lock:  # two first-password claims must not both win
        current = password_hash()
        if current:  # guesses are throttled: a stolen session must not be able to guess the password
            if not throttled_attempt(request, lambda: verify_password(body.current, current)):
                raise HTTPException(status_code=403, detail="current password is wrong")
        elif not throttled_attempt(request, lambda: check_setup_code(body.setup_code)):
            raise HTTPException(status_code=403, detail="setup code is wrong")
        set_password(body.new)
    set_session_cookie(request, response)  # this browser stays signed in, on the new epoch
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response, vibehealth_session: str | None = Cookie(default=None)) -> dict:
    """End this browser's session: the cookie is removed and the token itself stops working."""
    revoke_session(vibehealth_session)
    clear_session_cookie(request, response)
    return {"ok": True}


@router.post("/logout-all", dependencies=[Depends(require_session)])
def logout_all(request: Request, response: Response) -> dict:
    """End every session on every device, this one included."""
    end_all_sessions()
    clear_session_cookie(request, response)
    return {"ok": True}
