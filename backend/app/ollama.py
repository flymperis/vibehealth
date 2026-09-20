"""Local Ollama calls for the two readers (same request shapes as the benchmark)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

import httpx

log = logging.getLogger("vibehealth")

# The benchmarked prompt, unchanged.
PROMPT_A = """This image is one page of a Greek medical laboratory report (Affidea).
Extract every laboratory test result printed on this page, in reading order, as JSON.
Rules:
- name: the test name exactly as printed (Greek/Latin as printed, without the dotted leader).
- value: the result exactly as printed (number or text such as "Όχι", "0 - 1", "+"). Do not calculate or guess.
- unit: the unit as printed, or "" if none.
- reference_range: the reference range (Τ.Α. / Τιμές Αναφοράς) as printed, or "" if none.
- In the white-cell differential (Τύπος Λευκών Κυττάρων) output the percentage row, and the absolute count (Απολύτως column) as a separate row named "<name> (απόλυτος αριθμός)" with its unit and range.
- Include qualitative urine results. Skip tests whose result is blank.
- If the page is a patient-history page ("ΑΝΑΛΥΤΙΚΗ ΠΑΡΟΥΣΙΑΣΗ ΙΣΤΟΡΙΚΟΥ ΑΣΘΕΝΗ", tables of past dates with charts), return {"results": []}.
- If the page has no results, return {"results": []}."""

SCHEMA_A = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "value": {"type": "string"},
                       "unit": {"type": "string"}, "reference_range": {"type": "string"}},
        "required": ["name", "value", "unit", "reference_range"]}}},
    "required": ["results"],
}

PROMPT_B = "Text Recognition:"  # glm-ocr's native OCR task
NUM_PREDICT = 4096


class SafeError(Exception):
    """An error whose message we wrote ourselves: fine to show in the UI (no URL, no
    exception text from a library, no secret). Everything else is described by its class only."""


class OllamaError(SafeError):
    """A failure worth showing to the user as is."""


class PageError(SafeError):
    """This page could not be read (truncated or empty answer, bad JSON)."""


class StructuredOutputError(PageError):
    """The answer was not valid JSON for the schema: the model ignored the `format` constraint."""


BAD_JSON = "bad JSON in the answer (this model did not return structured output)"

# Models that answered "does not support thinking" to `think: false`: from then on (for the life of
# the process) the flag is left out for them. Keyed by the name as it was sent.
_NO_THINK: set[str] = set()


def describe(exc: BaseException) -> str:
    """What the UI may show about a failure: our own messages as they are, anything else
    as `Unexpected error (ClassName)` (library messages can carry URLs and paths)."""
    if isinstance(exc, SafeError):
        text = str(exc).strip()
        return text[:300] if text else type(exc).__name__
    return f"Unexpected error ({type(exc).__name__})"


def _status_message(code: int) -> str:
    """A fixed message for an HTTP error answer: bad request (4xx) or server error (5xx)."""
    if code >= 500:
        return f"Ollama error {code}: the server reported an internal error."
    return f"Ollama error {code}: the request was rejected (bad request)."


def _unreachable(exc: httpx.HTTPError) -> OllamaError:
    """A fixed message for a failed call (unreachable, timeout, HTTP error): the exception's text can carry
    the address. Every message shown or stored comes from a fixed class, never from the other end."""
    if isinstance(exc, httpx.TimeoutException):
        return OllamaError("Ollama did not answer in time.")
    if isinstance(exc, httpx.HTTPStatusError):
        return OllamaError(f"Ollama answered with HTTP {exc.response.status_code}.")
    return OllamaError("Ollama is not reachable at the configured address.")


_VERSION_RX = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+\-]{0,39}")

# Where the setup guide looks for a running Ollama. A FIXED list: the client never supplies an
# address to probe (that would make the server a port scanner for whoever can reach it).
DETECT_CANDIDATES = (
    "http://host.docker.internal:11434",
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://172.17.0.1:11434",  # the Docker bridge gateway (the host, seen from a container)
)


async def probe_version(url: str, timeout: float) -> str | None:
    """The version Ollama reports at `url`, or None when nothing that looks like Ollama answers
    (no address, no exception text in what comes back). Redirects are not followed."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/version")
            resp.raise_for_status()
        version = resp.json().get("version")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if isinstance(version, str) and _VERSION_RX.fullmatch(version):
        return version
    return None


async def detect(timeout: float = 1.5) -> list[dict]:
    """Which of the fixed candidate addresses answer like Ollama, in list order."""
    versions = await asyncio.gather(*(probe_version(u, timeout) for u in DETECT_CANDIDATES))
    return [{"url": u, "version": v} for u, v in zip(DETECT_CANDIDATES, versions) if v is not None]


def has_model(installed: list[str], name: str) -> bool:
    """"glm-ocr" and "glm-ocr:latest" are the same model to Ollama."""
    return name in installed or (":" not in name and f"{name}:latest" in installed)


class Ollama:
    def __init__(self, url: str, timeout: float, num_ctx: int, keep_alive: str) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.keep_alive = _keep_alive(keep_alive)

    async def models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.url}/api/tags")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        try:
            models = resp.json().get("models", [])
        except (ValueError, AttributeError):
            models = None
        if not isinstance(models, list):
            raise OllamaError("Ollama answered, but not with a list of models.")
        return sorted({m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)})

    async def capabilities(self, name: str) -> list[str] | None:
        """What Ollama says the model can do (`completion`, `vision`, `tools`, ...), or None when it
        does not say (an older Ollama, an error). `name` must come from `models()`, never from a client."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{self.url}/api/show", json={"model": name, "name": name})
                resp.raise_for_status()
            caps = resp.json().get("capabilities")
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        if isinstance(caps, list) and all(isinstance(c, str) for c in caps):
            return caps
        return None

    async def gpu_percent(self, name: str) -> int | None:
        """How much of the loaded model sits in GPU memory (`size_vram` / `size`), from /api/ps right after
        a run; None when the model is not listed or Ollama does not say."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.url}/api/ps")
                resp.raise_for_status()
            loaded = resp.json().get("models", [])
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        if not isinstance(loaded, list):
            return None
        for entry in loaded:
            if not isinstance(entry, dict) or name not in (entry.get("name"), entry.get("model")):
                continue
            size, vram = entry.get("size"), entry.get("size_vram")
            if isinstance(size, int | float) and isinstance(vram, int | float) and size > 0:
                return max(0, min(100, round(vram * 100 / size)))
        return None

    async def version(self) -> str:
        """The server's version, or "" when it does not say (the models call is the real check)."""
        return await probe_version(self.url, 10) or ""

    async def _chat(self, body: dict) -> dict:
        """POST /api/chat; one retry on a timeout, and one retry without `think` when the model
        says it does not support thinking (remembered for this model until the process ends)."""
        if body["model"] in _NO_THINK:
            body.pop("think", None)
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(f"{self.url}/api/chat", json=body)
            except httpx.TimeoutException as exc:
                if attempt == 1:
                    log.warning("ollama %s timed out, retrying once", body["model"])
                    continue
                raise OllamaError(
                    f"Ollama timed out twice after {int(self.timeout)} s ({body['model']})"
                ) from exc
            except httpx.HTTPError as exc:
                raise _unreachable(exc) from exc
            if resp.status_code == 404:  # (the name is our own setting, not the other end's text)
                raise OllamaError(f"model {body['model']} is not installed in Ollama")
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("error", "")
                except (ValueError, AttributeError):
                    detail = resp.text
                if resp.status_code == 400 and "think" in body and "does not support thinking" in str(detail).lower():
                    log.info("ollama %s does not support thinking: retrying without it", body["model"])
                    _NO_THINK.add(body["model"])
                    body.pop("think")
                    return await self._chat(body)
                # Only ever a fixed class: what the other end wrote (it may name paths, hosts or models) is
                # neither shown nor stored. The status code is a number we read, not text we pass on.
                raise OllamaError(_status_message(resp.status_code))
            try:
                answer = resp.json()
            except ValueError:
                answer = None
            if not isinstance(answer, dict):
                raise OllamaError("Ollama answered with something that is not a chat reply.")
            return answer
        raise AssertionError("unreachable")

    def _body(self, model: str, prompt: str, png: bytes) -> dict:
        return {
            "model": model,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": 0, "num_predict": NUM_PREDICT},
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(png).decode()]}],
        }

    @staticmethod
    def _content(r: dict) -> str:
        content = (r.get("message") or {}).get("content") or ""
        if r.get("done_reason") == "length":
            raise PageError("answer cut off (length)")
        if not content.strip():
            raise PageError("empty answer")
        return content

    async def read_rows(self, model: str, png: bytes) -> list[dict]:
        """Reader A: JSON rows {name, value, unit, reference_range}."""
        body = self._body(model, PROMPT_A, png)
        body["format"] = SCHEMA_A
        content = self._content(await self._chat(body))
        try:
            results = json.loads(content)["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise StructuredOutputError(BAD_JSON) from exc
        if not isinstance(results, list):
            raise StructuredOutputError(BAD_JSON)
        return [r for r in results if isinstance(r, dict)]

    async def read_text(self, model: str, png: bytes) -> str:
        """Reader B: plain OCR text."""
        return self._content(await self._chat(self._body(model, PROMPT_B, png)))


def _keep_alive(value: str) -> str | int:
    # Ollama wants a number for 0 / -1 / plain seconds, a duration string otherwise.
    return int(value) if value.lstrip("-").isdigit() else value
