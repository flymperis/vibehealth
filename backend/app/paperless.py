"""Paperless-ngx REST client (read-only).

Everything it needs (address, token, which documents count) is read from the
settings store when a `Paperless()` is made, so a change saved in the app applies
to the next call. The token is only ever sent to the address it was made for, is
never put in an error message, and the API's own `next` links are not followed
(pages are asked for by number), so a Paperless that answers with a foreign link
cannot make us send the token elsewhere.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import secret_store, settings_store

PAGE_SIZE = 100
_CHECK_TIMEOUT = httpx.Timeout(10.0)

ERROR_KINDS = ("connection", "unauthorized", "not_found", "tls", "timeout", "other")


@dataclass(frozen=True)
class Selection:
    """Which documents count as medical."""

    match: str  # type_or_tags | type_only | tags_only
    document_type: str
    tags: tuple[str, ...]

    @property
    def use_type(self) -> bool:
        return self.match in ("type_or_tags", "type_only") and bool(self.document_type.strip())

    @property
    def use_tags(self) -> bool:
        return self.match in ("type_or_tags", "tags_only") and any(t.strip() for t in self.tags)


def _origin(url: str) -> str:
    """scheme://host[:port] of a URL, with no credentials and no path: safe to show."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}" if host else "the address"
    except ValueError:
        return "the address"


def _is_tls(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 8:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        text = str(cur).upper()
        if "CERTIFICATE_VERIFY_FAILED" in text or "[SSL" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def classify(exc: BaseException, url: str = "") -> tuple[str, str]:
    """(error_kind, message a person can act on). Fixed strings: never the token, the
    exception's own text or the address (`url` is accepted for callers, and not used)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "unauthorized", (
                f"Paperless refused the API token (HTTP {code}). Check the token, and that its "
                "user may read documents."
            )
        if code == 404:
            return "not_found", (
                "Nothing was found at that address (HTTP 404). Check the URL, including any path prefix."
            )
        if 300 <= code < 400:
            return "other", f"The address redirects (HTTP {code}). Use the address it redirects to."
        return "other", f"Paperless answered with HTTP {code}."
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "Paperless did not answer in time."
    if isinstance(exc, httpx.TransportError):
        if isinstance(exc, httpx.UnsupportedProtocol):
            return "other", "The address is not usable. Use http:// or https://."
        if _is_tls(exc):
            return "tls", (
                "Could not make a secure connection to Paperless: the certificate or TLS setup was "
                "not accepted. Check the address (http or https) and the certificate."
            )
        return "connection", "Could not connect to Paperless. Check the address and that it is running."
    if isinstance(exc, httpx.InvalidURL):
        return "other", "The address is not usable."
    return "other", f"Unexpected error ({type(exc).__name__})."


_USERINFO_RX = re.compile(r"(?<=://)[^/@\s]+@")


def safe_text(text: str) -> str:
    """Error text for the UI or a log: no URL credentials, no secret the app has handled."""
    return secret_store.redact(_USERINFO_RX.sub("", text))


class Paperless:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        cfg = settings_store.resolve("paperless").values  # saved in the app > env > default
        saved_url = (cfg["url"] or "").rstrip("/")
        self.url = (url or saved_url).rstrip("/")
        if token:
            self.token = token
        elif url and self.url != saved_url:
            self.token = ""  # the saved token is never sent to an address it was not saved for
        else:
            self.token = cfg["token"]
        self.public_url = (cfg["public_url"] or "").rstrip("/")
        self.match = cfg["match"]
        self.document_type = cfg["document_type"]
        self.tag_names = tuple(cfg["tags"])
        self.kind_map = cfg["kind_map"]

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def selection(self) -> Selection:
        return Selection(self.match, self.document_type, self.tag_names)

    def _client(self, timeout: float | httpx.Timeout = 60) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Token {self.token}", "Accept": "application/json"},
            timeout=timeout,
        )

    async def _get_all(self, path: str, params: dict[str, Any]) -> list[dict]:
        """Every page of a list, asked for by page number until the last."""
        out: list[dict] = []
        async with self._client() as client:
            page = 1
            while True:
                query = {**params, "page_size": PAGE_SIZE}
                if page > 1:
                    query["page"] = page
                resp = await client.get(path, params=query)
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("results", []))
                if not data.get("next"):
                    return out
                page += 1

    async def _count(self, params: dict[str, Any], path: str = "/api/documents/") -> int:
        """How many rows match, from Paperless's own `count` (a page of one)."""
        async with self._client(_CHECK_TIMEOUT) as client:
            resp = await client.get(path, params={**params, "page_size": 1})
            resp.raise_for_status()
            return int(resp.json().get("count", 0))

    async def tags(self) -> dict[int, str]:
        rows = await self._get_all("/api/tags/", {})
        return {r["id"]: r["name"] for r in rows}

    async def document_types(self) -> dict[int, str]:
        rows = await self._get_all("/api/document_types/", {})
        return {r["id"]: r["name"] for r in rows}

    async def tag_ids(
        self, names: tuple[str, ...] | list[str], known: dict[int, str] | None = None
    ) -> list[int]:
        """Ids of the tags with these names (case ignored). With `known` (every tag)
        nothing is fetched; without it each name is looked up by itself, which stays
        cheap when the instance has thousands of tags."""
        wanted = {n.strip().lower() for n in names if n.strip()}
        if not wanted:
            return []
        if known is not None:
            return [tid for tid, name in known.items() if name.strip().lower() in wanted]

        async def one(name: str) -> list[int]:
            async with self._client(_CHECK_TIMEOUT) as client:
                resp = await client.get("/api/tags/", params={"name__iexact": name, "page_size": 10})
                resp.raise_for_status()
                return [
                    r["id"] for r in resp.json().get("results", []) if r["name"].strip().lower() == name
                ]

        found = await asyncio.gather(*(one(n) for n in sorted(wanted)))
        return list(dict.fromkeys(i for ids in found for i in ids))

    async def medical_documents(
        self, selection: Selection | None = None, known_tags: dict[int, str] | None = None
    ) -> list[dict]:
        """The documents that count, by the chosen `match` mode.

        type_or_tags (the original behaviour): the document type *or* one of the
        tags. Either half can go missing on its own: paperless-gpt has been seen
        clearing the document type of a lab report it re-processed, and a
        document scanned straight into a tag never gets the type at all.
        """
        sel = selection or self.selection()
        found: dict[int, dict] = {}

        if sel.use_type:
            for row in await self._get_all(
                "/api/documents/",
                {"document_type__name__iexact": sel.document_type, "ordering": "-created"},
            ):
                found[row["id"]] = row

        if sel.use_tags:
            known = known_tags if known_tags is not None else await self.tags()
            tag_ids = await self.tag_ids(sel.tags, known)
            if tag_ids:
                for row in await self._get_all(
                    "/api/documents/",
                    {"tags__id__in": ",".join(str(t) for t in tag_ids), "ordering": "-created"},
                ):
                    found.setdefault(row["id"], row)

        return list(found.values())

    async def count_documents(self, selection: Selection | None = None) -> int:
        """How many documents `medical_documents` would return, without fetching them."""
        sel = selection or self.selection()
        type_params = {"document_type__name__iexact": sel.document_type} if sel.use_type else None
        tag_params = None
        if sel.use_tags:
            ids = await self.tag_ids(sel.tags)
            if ids:
                tag_params = {"tags__id__in": ",".join(str(t) for t in ids)}
        if type_params and tag_params:  # either one: A + B - both
            a, b, both = await asyncio.gather(
                self._count(type_params),
                self._count(tag_params),
                self._count({**type_params, **tag_params}),
            )
            return a + b - both
        params = type_params or tag_params
        return await self._count(params) if params else 0

    async def named(self, what: str, q: str = "", limit: int = 100) -> tuple[list[dict], int]:
        """One page of tags or document types by name, with how many documents use each,
        and how many exist in all. `q` filters by name (case ignored)."""
        params: dict[str, Any] = {"page_size": limit, "ordering": "name"}
        if q:
            params["name__icontains"] = q
        async with self._client(_CHECK_TIMEOUT) as client:
            resp = await client.get(f"/api/{what}/", params=params)
            resp.raise_for_status()
            data = resp.json()
        rows = [
            {"id": r["id"], "name": r["name"], "count": int(r.get("document_count") or 0)}
            for r in data.get("results", [])
        ]
        return rows[:limit], int(data.get("count", len(rows)))

    async def check(self) -> dict:
        """Can we reach Paperless with this address and token? Never raises."""
        if not self.url:
            return _failed("other", "No Paperless URL is set.")
        if not self.token:
            return _failed("unauthorized", "No API token is set.")
        try:
            async with self._client(_CHECK_TIMEOUT) as client:
                resp = await client.get("/api/documents/", params={"page_size": 1})
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError:
                    data = None
        except Exception as exc:  # noqa: BLE001 - classified, and shown without its own text
            return _failed(*classify(exc, self.url))
        if not isinstance(data, dict) or "results" not in data:
            return _failed(
                "other",
                "The address answered, but it does not look like Paperless-ngx (is the URL right?).",
            )
        version = (resp.headers.get("x-version") or "").strip()
        if not re.fullmatch(r"[\w.+\-]{1,40}", version):
            version = ""
        return {"ok": True, "version": version or None, "error_kind": None, "error": None}

    async def document(self, paperless_id: int) -> dict:
        async with self._client() as client:
            resp = await client.get(f"/api/documents/{paperless_id}/")
            resp.raise_for_status()
            return resp.json()

    async def thumbnail(self, paperless_id: int) -> tuple[bytes, str]:
        """Fetched through us so the browser never needs a Paperless session."""
        async with self._client() as client:
            resp = await client.get(f"/api/documents/{paperless_id}/thumb/")
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "image/webp")

    async def preview(self, paperless_id: int) -> tuple[bytes, str]:
        async with self._client() as client:
            resp = await client.get(f"/api/documents/{paperless_id}/preview/")
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "application/pdf")

    async def download(self, paperless_id: int, original: bool = True) -> tuple[bytes, str]:
        """The file itself, kept in memory by the caller and never stored."""
        async with self._client() as client:
            resp = await client.get(
                f"/api/documents/{paperless_id}/download/",
                params={"original": "true"} if original else None,
                timeout=120,
            )
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "")

    @staticmethod
    def public_link(paperless_id: int) -> str:
        values = settings_store.resolve("paperless").values
        base = (values["public_url"] or values["url"] or "").rstrip("/")  # no public address: use the API's
        return f"{base}/documents/{paperless_id}/details"


def _failed(kind: str, message: str) -> dict:
    return {"ok": False, "version": None, "error_kind": kind, "error": message}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat, lambda v: datetime.strptime(v, "%Y-%m-%d")):
        try:
            return parse(text).date()
        except ValueError:
            continue
    return None
