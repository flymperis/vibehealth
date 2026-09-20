"""The Ollama client against a mock transport (no network)."""

import asyncio
import json

import httpx
import pytest

from app import ollama
from app.ollama import Ollama, OllamaError, PageError, describe


REAL_CLIENT = httpx.AsyncClient


def use_transport(monkeypatch, handler):
    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ollama.httpx, "AsyncClient", factory)


def client():
    return Ollama("http://ollama.test:11434/", 30, 8192, "5m")


def test_reader_a_request_shape(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done_reason": "stop", "message": {
            "content": json.dumps({"results": [{"name": "Ουρία", "value": "30", "unit": "", "reference_range": ""}]})}})

    use_transport(monkeypatch, handler)
    rows = asyncio.run(client().read_rows("qwen3.5:4b", b"png"))
    assert rows[0]["value"] == "30"
    body = seen[0]
    assert body["think"] is False and body["stream"] is False
    assert body["format"]["required"] == ["results"]
    assert body["options"] == {"num_ctx": 8192, "temperature": 0, "num_predict": 4096}
    assert body["keep_alive"] == "5m"
    assert body["messages"][0]["images"] == ["cG5n"]


def test_reader_b_has_no_format(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done_reason": "stop", "message": {"content": "text"}})

    use_transport(monkeypatch, handler)
    assert asyncio.run(client().read_text("glm-ocr", b"x")) == "text"
    assert "format" not in seen[0] and seen[0]["messages"][0]["content"] == "Text Recognition:"


@pytest.mark.parametrize("reply", [
    {"done_reason": "length", "message": {"content": "{\"results\": ["}},
    {"done_reason": "stop", "message": {"content": "  "}},
    {"done_reason": "stop", "message": {"content": "not json"}},
])
def test_page_errors(monkeypatch, reply):
    use_transport(monkeypatch, lambda r: httpx.Response(200, json=reply))
    with pytest.raises(PageError):
        asyncio.run(client().read_rows("qwen3.5:4b", b"x"))


def test_timeout_is_retried_once(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json={"message": {"content": "ok"}})

    use_transport(monkeypatch, handler)
    assert asyncio.run(client().read_text("glm-ocr", b"x")) == "ok"

    calls.clear()

    def always(request):
        calls.append(1)
        raise httpx.ReadTimeout("")

    use_transport(monkeypatch, always)
    with pytest.raises(OllamaError, match="timed out twice"):
        asyncio.run(client().read_text("glm-ocr", b"x"))
    assert len(calls) == 2


def test_unreachable_and_missing_model(monkeypatch):
    def down(request):
        raise httpx.ConnectError("")

    use_transport(monkeypatch, down)
    with pytest.raises(OllamaError) as excinfo:
        asyncio.run(client().models())
    assert str(excinfo.value) == "Ollama is not reachable at the configured address."
    assert "ollama.test" not in str(excinfo.value)  # no address in what the UI shows

    use_transport(monkeypatch, lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(OllamaError, match="not installed"):
        asyncio.run(client().read_text("nope", b"x"))


def test_keep_alive_numbers():
    assert Ollama("http://x", 1, 2048, "0").keep_alive == 0
    assert Ollama("http://x", 1, 2048, "-1").keep_alive == -1
    assert Ollama("http://x", 1, 2048, "10m").keep_alive == "10m"


@pytest.mark.parametrize("body", [b"<html>nope", b"[]", b'{"models": null}', b'{"models": "x"}',
                                  b'{"models": {"a": 1}}', b"5", b"null"])
def test_models_survives_an_odd_reply(monkeypatch, body):
    use_transport(monkeypatch, lambda r: httpx.Response(200, content=body))
    with pytest.raises(OllamaError, match="not with a list of models"):
        asyncio.run(client().models())


def test_models_skips_entries_without_a_name(monkeypatch):
    reply = {"models": [{"name": "b:1"}, {"nom": "x"}, "str", None, {"name": 7}, {"name": "a:2"}, {"name": "b:1"}]}
    use_transport(monkeypatch, lambda r: httpx.Response(200, json=reply))
    assert asyncio.run(client().models()) == ["a:2", "b:1"]


def test_errors_are_fixed_strings_without_addresses(monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(500, json={"error": "cannot reach http://198.51.100.7:1234/x now"}))
    with pytest.raises(OllamaError) as excinfo:
        asyncio.run(client().read_text("glm-ocr", b"x"))
    assert "198.51.100.7" not in str(excinfo.value) and "Ollama error 500" in str(excinfo.value)
    assert "cannot reach" not in str(excinfo.value)  # what the other end wrote is not passed on at all

    def timeout(request):
        raise httpx.ConnectTimeout("http://secret.internal:1")

    use_transport(monkeypatch, timeout)
    with pytest.raises(OllamaError) as excinfo:
        asyncio.run(client().models())
    assert "secret.internal" not in str(excinfo.value) and "in time" in str(excinfo.value)


@pytest.mark.parametrize("body", [b"plain text", b"[1, 2]", b"null"])
def test_chat_reply_that_is_not_an_object_is_an_ollama_error(monkeypatch, body):
    use_transport(monkeypatch, lambda r: httpx.Response(200, content=body))
    with pytest.raises(OllamaError):
        asyncio.run(client().read_text("glm-ocr", b"x"))


@pytest.mark.parametrize("status,expected", [
    (400, "Ollama error 400: the request was rejected (bad request)."),
    (401, "Ollama error 401: the request was rejected (bad request)."),
    (500, "Ollama error 500: the server reported an internal error."),
    (503, "Ollama error 503: the server reported an internal error."),
])
def test_an_error_answer_maps_to_a_fixed_class_whatever_it_says(monkeypatch, status, expected):
    hostile = "/home/secret/models/x.gguf failed on http://198.51.100.7:1/x <script>"
    use_transport(monkeypatch, lambda r: httpx.Response(status, json={"error": hostile}))
    with pytest.raises(OllamaError) as excinfo:
        asyncio.run(client().read_text("glm-ocr", b"x"))
    assert str(excinfo.value) == expected
    assert describe(excinfo.value) == expected
