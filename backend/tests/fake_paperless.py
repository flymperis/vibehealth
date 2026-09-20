"""A small in-process Paperless-ngx for tests, served through httpx.MockTransport."""

from __future__ import annotations

import httpx

TOKEN = "synthetic-paperless-token-0123456789"
BASE = "http://paperless.test:8000"


class FakePaperless:
    def __init__(self, token: str = TOKEN, version: str = "2.14.7") -> None:
        self.token = token
        self.version = version
        self.tags = {1: "Blood Test", 2: "Medical Report", 3: "Prescription", 4: "Imaging", 5: "Receipt"}
        self.types = {10: "Medical", 11: "Invoice"}
        self.docs: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.next_host = None  # make `next` links point at another host
        self.fail: Exception | None = None  # raised by every request
        self.status_override: int | None = None
        self.body_override: httpx.Response | None = None

    def doc(self, pk: int, title: str, type_id: int | None = None, tags: tuple[int, ...] = (), created="2024-05-01"):
        self.docs.append({
            "id": pk, "title": title, "document_type": type_id, "tags": list(tags),
            "created": created + "T00:00:00Z", "created_date": created,
        })

    def standard(self) -> "FakePaperless":
        """Three documents that an unchanged install syncs, and two it ignores."""
        self.doc(1, "Lab results", 10, (1,), "2024-05-01")  # type and tag
        self.doc(2, "Scanned straight into a tag", None, (2,), "2024-04-01")  # tag only
        self.doc(3, "Typed only", 10, (), "2024-03-01")  # type only
        self.doc(4, "Electricity bill", 11, (5,), "2024-02-01")  # not medical
        self.doc(5, "Untagged", None, (), "2024-01-01")
        return self

    # -- wiring -------------------------------------------------------------

    def install(self, monkeypatch) -> "FakePaperless":
        real = httpx.AsyncClient
        transport = httpx.MockTransport(self.handle)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
        return self

    def doc_queries(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path == "/api/documents/"]

    # -- the API ------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"detail": "x"})
        if self.body_override is not None:
            return self.body_override
        if request.headers.get("authorization") != f"Token {self.token}":
            return httpx.Response(401, json={"detail": "Invalid token."})
        p = request.url.params
        path = request.url.path
        headers = {"X-Version": self.version}
        if path == "/api/documents/":
            rows = self.docs
            if "document_type__name__iexact" in p:
                ids = {i for i, n in self.types.items() if n.lower() == p["document_type__name__iexact"].lower()}
                rows = [d for d in rows if d["document_type"] in ids]
            if "tags__id__in" in p:
                want = {int(x) for x in p["tags__id__in"].split(",")}
                rows = [d for d in rows if want & set(d["tags"])]
            rows = sorted(rows, key=lambda d: d["created"], reverse=True)
            return self._page(request, rows, headers)
        if path in ("/api/tags/", "/api/document_types/"):
            table = self.tags if path == "/api/tags/" else self.types
            key = "tags" if path == "/api/tags/" else "document_type"
            rows = []
            for pk, name in sorted(table.items(), key=lambda kv: kv[1].lower()):
                if "name__iexact" in p and name.lower() != p["name__iexact"].lower():
                    continue
                if "name__icontains" in p and p["name__icontains"].lower() not in name.lower():
                    continue
                n = sum(1 for d in self.docs if (pk in d[key] if key == "tags" else d[key] == pk))
                rows.append({"id": pk, "name": name, "document_count": n})
            return self._page(request, rows, headers)
        return httpx.Response(404, json={"detail": "Not found."})

    def _page(self, request: httpx.Request, rows: list[dict], headers: dict) -> httpx.Response:
        p = request.url.params
        size = int(p.get("page_size", 25))
        page = int(p.get("page", 1))
        chunk = rows[(page - 1) * size: page * size]
        nxt = None
        if page * size < len(rows):
            host = self.next_host or f"{request.url.scheme}://{request.url.host}:{request.url.port or 80}"
            nxt = f"{host}{request.url.path}?page={page + 1}&page_size={size}"
        return httpx.Response(
            200, headers=headers, json={"count": len(rows), "next": nxt, "previous": None, "results": chunk}
        )
