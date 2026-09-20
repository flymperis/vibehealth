"""Settings saved in the app, by section. Secrets are write-only.

GET returns the values, where each comes from ("app", "env" or "default") and,
for a secret, only `<name>_set`, `<name>_last4` and `<name>_source`.
PUT takes a partial body: a value sets it, `null` removes the saved value (the
environment or default answers again); a secret is a string (set) or "" (clear);
anything left out stays as it is.

Changing an address the server connects to, or an origin / host it trusts (see
`settings_store.SENSITIVE`), is refused while no password is set and needs
`current_password` in the body once one is.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import settings_store as store
from ..security import confirm_sensitive_change, require_session
from ..worker import settings_changed

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_session)])


def _section(name: str) -> store.Section:
    section = store.SECTIONS.get(name)
    if section is None or not section.api:
        raise HTTPException(404, "not found")
    return section


@router.get("")
def get_all() -> dict:
    return {s.name: store.public_view(s) for s in store.SECTIONS.values() if s.api}


@router.get("/{section}")
def get_section(section: str) -> dict:
    return store.public_view(_section(section))


@router.put("/{section}")
def put_section(section: str, body: dict, request: Request) -> dict:
    definition = _section(section)
    body = dict(body)
    confirmation = body.pop("current_password", None)  # a request field, never a setting
    try:
        if store.sensitive_changes(definition, body):
            store.update(definition, body, dry_run=True)  # unusable input is a 422 before anything else
            confirm_sensitive_change(request, confirmation)
        store.update(definition, body)
    except store.SettingsError as exc:
        raise HTTPException(422, exc.errors) from exc
    if definition.name == "paperless":
        settings_changed()  # the sync loop applies it now, not at its next wake-up
    return store.public_view(definition)
