"""Helpers for the Paperless settings page: test the connection, list what can be chosen,
and count what a choice would sync.

Each of these makes the server open a connection, which makes them a way to probe the
network from the server's position. So they need a session, they are rate limited, they
never follow redirects, and their answers carry classified, fixed messages instead of
whatever the other end said. A token typed into the test is used for that one call only,
and never returned or logged.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .. import settings_store as store
from ..paperless import Paperless, Selection, classify
from ..security import OPEN_MODE_REFUSAL, confirm_sensitive_change, password_hash, require_session
from ..throttle import paperless_limiter


def limited() -> None:
    wait = paperless_limiter.hit()
    if wait:
        raise HTTPException(
            429, f"Too many requests. Try again in {wait} seconds.", headers={"Retry-After": str(wait)}
        )


# The session is checked first, so requests without one do not use up the allowance.
router = APIRouter(
    prefix="/api/paperless", tags=["paperless"], dependencies=[Depends(require_session), Depends(limited)]
)


class ConnectionBody(BaseModel):
    url: str | None = None  # a value not saved yet
    token: str | None = None  # "" or absent: the saved token (only for the saved URL)
    current_password: str | None = None  # needed to try an address other than the saved one

    model_config = {"extra": "forbid"}


class DiscoverBody(BaseModel):
    q: str = Field("", max_length=100)  # only names containing this (case ignored)
    limit: int = Field(100, ge=1, le=200)  # per list

    model_config = {"extra": "forbid"}


class PreviewBody(BaseModel):
    """A candidate choice of documents; whatever is left out is taken from the saved settings."""

    match: Literal["type_or_tags", "type_only", "tags_only"] | None = None
    document_type: str | None = Field(None, max_length=100)
    tags: list[str] | None = Field(None, max_length=50)

    model_config = {"extra": "forbid"}


def _bad(field: str, message: str) -> HTTPException:
    return HTTPException(422, [{"field": field, "message": message}])


def _upstream(exc: Exception, url: str) -> HTTPException:
    kind, message = classify(exc, url)
    return HTTPException(502, {"error_kind": kind, "error": message})


def _connected() -> Paperless:
    paperless = Paperless()
    if not paperless.configured:
        raise HTTPException(
            409,
            {"error_kind": "other", "error": "Paperless is not connected. Save the address and the token first."},
        )
    return paperless


@router.post("/test")
async def test_connection(request: Request, body: ConnectionBody | None = None) -> dict:
    """Can the server reach Paperless? Without a body: with the saved settings. With
    `url` and/or `token`: with those, unsaved. Always 200 with `ok`; 422 for input that
    cannot be used, including a new `url` without a `token` (the saved token is not
    sent to an address it was not saved for). While no password is set a different `url`
    or any `token` is refused with 403: only the saved settings can be tested. With a password set, a
    `url` other than the saved one also needs `current_password` (as saving it does)."""
    body = body or ConnectionBody()
    saved_url = Paperless().url
    url = token = None
    if body.url is not None:
        try:
            url = store.clean_url(body.url)
        except ValueError as exc:
            raise _bad("url", str(exc)) from None
        if not url:
            raise _bad("url", "Enter the Paperless address.")
    if body.token:
        try:
            token = store.clean_secret(body.token)
        except ValueError as exc:
            raise _bad("token", str(exc)) from None
    if (url not in (None, saved_url) or token is not None) and not password_hash():
        # Open instance: only the saved address and token may be tried. An address or token
        # from the request would let any visitor aim the server, and a token, elsewhere.
        raise HTTPException(403, OPEN_MODE_REFUSAL)
    if url not in (None, saved_url):
        confirm_sensitive_change(request, body.current_password)
    if url is not None and url != saved_url and token is None:
        raise _bad(
            "token",
            "The address differs from the saved one, so enter the token too: the saved token is not sent to it.",
        )
    return await Paperless(url=url, token=token).check()


@router.post("/discover")
async def discover(body: DiscoverBody | None = None) -> dict:
    """Document types and tags in Paperless, with how many documents use each. One page
    of at most `limit` (default 100, max 200) per list, by name, optionally filtered by `q`;
    `total` is how many exist, `truncated` says the list is not all of them."""
    body = body or DiscoverBody()
    paperless = _connected()
    q = body.q.strip()
    try:
        (types, types_total), (tags, tags_total) = await asyncio.gather(
            paperless.named("document_types", q, body.limit), paperless.named("tags", q, body.limit)
        )
    except Exception as exc:  # noqa: BLE001 - classified; the text of the error is not passed on
        raise _upstream(exc, paperless.url) from None
    return {
        "document_types": types,
        "tags": tags,
        "total": {"document_types": types_total, "tags": tags_total},
        "truncated": {"document_types": types_total > len(types), "tags": tags_total > len(tags)},
    }


@router.post("/preview-count")
async def preview_count(body: PreviewBody | None = None) -> dict:
    """How many documents this choice would sync, from Paperless's own count."""
    body = body or PreviewBody()
    given = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        clean = store.PaperlessSection(**given)  # the same cleaning the settings get
    except ValidationError as exc:
        raise HTTPException(
            422,
            [{"field": str(e["loc"][0]), "message": e["msg"].removeprefix("Value error, ")} for e in exc.errors()],
        ) from None
    paperless = _connected()
    chosen = Selection(
        clean.match if "match" in given else paperless.match,
        clean.document_type if "document_type" in given else paperless.document_type,
        tuple(clean.tags if "tags" in given else paperless.tag_names),
    )
    if store.selection_problem(chosen.match, chosen.document_type, list(chosen.tags)):
        return {"count": 0, "empty_selection": True}
    try:
        return {"count": await paperless.count_documents(chosen), "empty_selection": False}
    except Exception as exc:  # noqa: BLE001
        raise _upstream(exc, paperless.url) from None
