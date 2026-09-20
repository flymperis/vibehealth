"""The model check: the registry, the expected-values parser, the endpoint and the run, with a fake Ollama.

Every file and value here is synthetic. What the tests care about: nothing is saved but counts (no document,
value or run row, no file left behind, no values or names in the stored history), the refusals (no password,
a reading in progress, a model that is not installed or cannot see), and the `think` fallback.
"""

import asyncio
import json
import os

import httpx
import pytest
from conftest import PasswordClient
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select
from upload_helpers import pdf

from app import model_check, ollama, settings_store, uploads
from app.db import engine
from app.main import app
from app.models import AppSetting, Document, ExtractedValue, ExtractionRun
from app.ollama import Ollama, OllamaError, PageError, StructuredOutputError
from app.worker import check_state, state

client = PasswordClient(app)

ROWS = [
    {"name": "Ουρία", "value": "30", "unit": "mg/dl", "reference_range": "10 - 50"},
    {"name": "Κάλιο", "value": "4,4", "unit": "mmol/l", "reference_range": "3.5 - 5.1"},
    {"name": "Σάκχαρο", "value": "95", "unit": "mg/dl", "reference_range": "70 - 110"},
    {"name": "Ψευδοεξέταση", "value": "7", "unit": "", "reference_range": ""},  # not a known test
]
TEXT_B = "Ουρία ....... 30 mg/dl 10 - 50\nΚάλιο ....... 4.5 mmol/l 3.5 - 5.1"


class FakeOllama:
    caps: dict[str, list[str] | None] = {}
    installed = ["qwen3.5:4b", "glm-ocr:latest", "plain-text:1b"]
    rows = ROWS
    fail: Exception | None = None
    gpu = 100
    delay = 0.0
    calls: list[str] = []

    def __init__(self, *args):
        pass

    async def models(self):
        return list(self.installed)

    async def capabilities(self, name):
        return self.caps.get(name, ["completion", "vision"])

    async def gpu_percent(self, name):
        return self.gpu

    async def read_rows(self, model, png):
        FakeOllama.calls.append(model)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return list(self.rows)

    async def read_text(self, model, png):
        FakeOllama.calls.append(model)
        return TEXT_B


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(model_check, "Ollama", FakeOllama)
    monkeypatch.setattr(FakeOllama, "caps", {"plain-text:1b": ["completion"]})
    monkeypatch.setattr(FakeOllama, "installed", ["qwen3.5:4b", "glm-ocr:latest", "plain-text:1b"])
    monkeypatch.setattr(FakeOllama, "rows", ROWS)
    monkeypatch.setattr(FakeOllama, "fail", None)
    monkeypatch.setattr(FakeOllama, "gpu", 100)
    monkeypatch.setattr(FakeOllama, "delay", 0.0)
    FakeOllama.calls = []
    started: list = []
    # the endpoint hands the work to a background task; here it is run by hand, where the test can see it
    monkeypatch.setattr(model_check, "start", lambda path, plan, progress: started.append((path, plan, progress)))
    return started


def post(model="qwen3.5:4b", data=None, file=None, **fields):
    files = {"file": ("secret-labs.pdf", file if file is not None else pdf(2), "application/pdf")}
    return client.post("/api/reading/model-check", files=files, data={"model": model, **(data or {}), **fields})


def run(started):
    """Run the task the endpoint would have started; returns check_state['last']."""
    (path, plan, progress), = started
    assert os.path.isfile(path)  # kept until the check is done
    asyncio.run(model_check._run(path, plan, progress))
    assert not os.path.exists(path)
    return check_state["last"]


def tmp_files():
    folder = os.path.join(uploads._base(), ".tmp")
    return os.listdir(folder) if os.path.isdir(folder) else []


def counts():
    with Session(engine) as s:
        return [s.exec(select(func.count()).select_from(t)).one() for t in (Document, ExtractedValue, ExtractionRun)]


EXPECTED = "Ουρία = 30 mg/dl\nUREA-not-a-code\nK: 4.4\nΣάκχαρο = 99 mg/dl\nΚρεατινίνη = 0,9 mg/dl"


# --- the registry ------------------------------------------------------------------------------------------


def test_registry_statuses():
    info = model_check.model_info
    assert info("qwen3.5:4b", ["completion", "vision"])["status"] == "tested"
    assert info("qwen3.5:4b", ["vision"])["role"] == "reader_a"
    assert info("glm-ocr:latest", ["vision"])["role"] == "reader_b"
    assert info("glm-ocr", None)["status"] == "tested"  # capabilities unknown: not a reason to refuse
    assert info("qwen3-vl:4b-instruct", ["vision"])["status"] == "not_recommended"
    assert info("someone/paddleocr-vl:latest", ["vision"])["status"] == "not_recommended"
    assert info("qwen3-vl:8b", ["vision"])["status"] == "untested"
    assert info("llava:7b", ["vision"])["status"] == "untested" and info("llava:7b", ["vision"])["tested"] is False
    text_only = info("llama3.2:3b", ["completion"])
    assert text_only["status"] == "no_vision" and text_only["usable"] is False and text_only["vision"] is False
    assert info("llama3.2:3b", None)["usable"] is True and info("llama3.2:3b", None)["vision"] is None
    assert info("qwen3.5:4b", ["completion"])["status"] == "no_vision"  # what Ollama says beats the registry
    assert info("glm-ocr", None)["glm_like"] and not info("qwen3.5:4b", None)["glm_like"]


def test_models_endpoint(monkeypatch):
    class Listing(FakeOllama):
        pass

    monkeypatch.setattr("app.routers.reading.Ollama", Listing)
    body = client.get("/api/reading/models").json()
    by_name = {m["name"]: m for m in body["models"]}
    assert body["ok"] and by_name["qwen3.5:4b"]["status"] == "tested"
    assert by_name["plain-text:1b"]["status"] == "no_vision" and not by_name["plain-text:1b"]["usable"]
    assert body["checks"] == {}

    class Down(FakeOllama):
        async def models(self):
            raise OllamaError("Ollama is not reachable at the configured address.")

    monkeypatch.setattr("app.routers.reading.Ollama", Down)
    assert client.get("/api/reading/models").json()["ok"] is False


# --- the expected list -------------------------------------------------------------------------------------


def test_parse_expected_formats_and_names():
    entries, warnings = model_check.parse_expected(
        "# a comment\n"
        "Ουρία = 30 mg/dl\n"
        "K: 4,4\n"
        "Glucose = 95\n"
        "Some Unknown Test = 12,5 U/L\n"
        "Αντίδραση (pH) = Όξινη\n"
        "\n"
        "no separator here\n"
        "= 5\n"
        "UREA: 31\n"
    )
    assert [(e.key, e.value) for e in entries[:3]] == [("UREA", "30"), ("K", "4,4"), ("GLU", "95")]
    assert entries[3].key.startswith("?") and entries[3].value == "12,5"  # unknown to the catalogue: by its name
    assert entries[4].value == "Όξινη"
    assert len(entries) == 5
    # a skipped line and a repeated test are named by number, never quoted
    assert any("line 8" in w for w in warnings) and any("line 9" in w for w in warnings)
    assert any("line 10" in w and "already listed" in w for w in warnings)
    assert not any("no separator" in w for w in warnings)


def test_an_overlong_expected_line_is_skipped_and_the_line_count_is_capped():
    long_line = "Ουρία = " + "9" * model_check.MAX_EXPECTED_LINE_CHARS
    entries, warnings = model_check.parse_expected("K: 4,4\n" + long_line + "\nGlucose = 95")
    assert [e.key for e in entries] == ["K", "GLU"]
    assert any("line 2" in w and "longer than" in w for w in warnings) and not any("999" in w for w in warnings)
    many = "\n".join(f"Test{i} = {i}" for i in range(model_check.MAX_EXPECTED_LINES + 50))
    entries, warnings = model_check.parse_expected(many)
    assert len(entries) <= model_check.MAX_EXPECTED_LINES and any("first" in w for w in warnings)


def test_score_counts():
    rows = [model_check.ReaderRow(r["name"], r["value"], r["unit"], r["reference_range"], 1) for r in ROWS]
    entries, _ = model_check.parse_expected(EXPECTED)
    assert model_check.score(rows, entries) == {"matched": 2, "wrong_value": 1, "missing": 1, "extra": 1}
    # the decimal comma and the unit do not matter; an unknown name matches the same printed name
    entries, _ = model_check.parse_expected("Ψευδοεξέταση = 7 mg\nΚάλιο = 4.4")
    assert model_check.score(rows, entries) == {"matched": 2, "wrong_value": 0, "missing": 0, "extra": 2}


# --- the endpoint and the run ------------------------------------------------------------------------------


def test_a_check_saves_nothing_but_counts(fakes):
    before = counts()
    r = post(data={"expected": EXPECTED, "use_reader_b": "true"})
    assert r.status_code == 202 and r.json() == {"started": True}
    assert check_state["current"] is not None  # the place is taken while the task has it
    last = run(fakes)
    assert check_state["current"] is None and last["status"] == "done"
    result = last["result"]
    assert result["model"] == "qwen3.5:4b" and result["pages"] == 2
    assert result["rows_found"] == 4 and result["expected_count"] == 4
    assert (result["matched"], result["wrong_value"], result["missing"], result["extra_not_in_expected"]) == (2, 1, 1, 1)
    assert result["verified_by_two_readers"] == 1  # urea: both agree; potassium 4,4 against 4.5 does not
    assert result["gpu"] == {"size_vram_percent": 100} and result["seconds_per_page"] >= 0
    assert FakeOllama.calls.count("glm-ocr") == 2  # reader B ran once per page, with the settings' model
    # nothing was created, nothing is left on disk
    assert counts() == before == [0, 0, 0]
    assert tmp_files() == []
    # the history holds counts and the model name, and no value, name or file name
    with Session(engine) as s:
        raw = s.get(AppSetting, "reading.model_checks").value
    stored = json.loads(raw)
    assert list(stored) == ["qwen3.5:4b"]
    assert set(stored["qwen3.5:4b"]) == {"date", "pages", "seconds_per_page", "rows_found", "expected_count",
                                         "matched", "wrong_value", "extra", "gpu_percent"}
    assert "Ουρία" not in raw and "secret-labs" not in raw and "mg/dl" not in raw
    assert client.get("/api/reading/settings").status_code == 200  # the extra key does not disturb the settings
    assert client.get("/api/reading/models").json()["checks"]["qwen3.5:4b"]["matched"] == 2


def test_without_expected_values_only_the_reading_is_reported(fakes):
    assert post().status_code == 202
    result = run(fakes)["result"]
    assert result["expected_count"] == 0 and result["extra_not_in_expected"] is None
    assert result["verified_by_two_readers"] is None and result["reader_b_model"] is None
    assert FakeOllama.calls == ["qwen3.5:4b"] * 2


def test_partial_gpu_is_a_warning(fakes, monkeypatch):
    monkeypatch.setattr(FakeOllama, "gpu", 40)
    post()
    result = run(fakes)["result"]
    assert result["gpu"] == {"size_vram_percent": 40}
    assert any("40%" in w for w in result["warnings"])


def test_the_file_is_removed_when_the_check_fails(fakes, monkeypatch):
    monkeypatch.setattr(FakeOllama, "fail", OllamaError("Ollama is not reachable at the configured address."))
    post()
    last = run(fakes)
    assert last["status"] == "error" and "not reachable" in last["error"]
    assert tmp_files() == [] and counts() == [0, 0, 0] and check_state["current"] is None
    with Session(engine) as s:
        assert s.get(AppSetting, "reading.model_checks") is None  # a failed check leaves no history


def test_an_unexpected_error_is_a_fixed_message(fakes, monkeypatch):
    monkeypatch.setattr(FakeOllama, "fail", ValueError("boom at http://198.51.100.5:1/secret-path"))
    post()
    last = run(fakes)
    assert last["status"] == "error" and "198.51.100.5" not in last["error"] and "boom" not in last["error"]
    assert tmp_files() == []


def test_a_model_that_does_not_return_json_gets_a_helpful_error(fakes, monkeypatch):
    monkeypatch.setattr(FakeOllama, "fail", StructuredOutputError(ollama.BAD_JSON))
    post()
    last = run(fakes)
    assert last["status"] == "error" and "structured output" in last["error"]


def test_a_page_error_is_a_warning_when_other_pages_read(fakes, monkeypatch):
    class Flaky(FakeOllama):
        n = 0

        async def read_rows(self, model, png):
            Flaky.n += 1
            if Flaky.n == 1:
                raise PageError("answer cut off (length)")
            return list(ROWS)

    monkeypatch.setattr(model_check, "Ollama", Flaky)
    post()
    result = run(fakes)["result"]
    assert result["rows_found"] == 4 and any("page 1" in w and "cut off" in w for w in result["warnings"])


def test_the_check_has_a_hard_timeout(fakes, monkeypatch):
    monkeypatch.setattr(model_check, "HARD_TIMEOUT", 0.2)
    monkeypatch.setattr(FakeOllama, "delay", 5)
    post()
    last = run(fakes)
    assert last["status"] == "error" and "too long" in last["error"]
    assert tmp_files() == [] and check_state["current"] is None


def test_only_the_first_pages_are_read(fakes):
    post(file=pdf(7))
    result = run(fakes)["result"]
    assert result["pages"] == model_check.MAX_PAGES and any("first 5 of 7" in w for w in result["warnings"])


def test_history_keeps_the_last_ten_models_newest_last():
    for i in range(12):
        model_check.remember({"model": f"m{i}:1b", "pages": 1, "seconds_per_page": 1.0, "rows_found": 1,
                              "expected_count": 0, "matched": 0, "wrong_value": 0, "extra_not_in_expected": None,
                              "gpu": None})
    model_check.remember({"model": "m5:1b", "pages": 2, "seconds_per_page": 2.0, "rows_found": 1,
                          "expected_count": 0, "matched": 0, "wrong_value": 0, "extra_not_in_expected": None,
                          "gpu": {"size_vram_percent": 90}})
    kept = model_check.history()
    assert list(kept) == [f"m{i}:1b" for i in (2, 3, 4, 6, 7, 8, 9, 10, 11, 5)]  # the oldest two dropped, m5 newest
    assert kept["m5:1b"]["gpu_percent"] == 90 and len(kept) == 10


# --- refusals ----------------------------------------------------------------------------------------------


def test_refused_while_no_password_is_set():
    plain = TestClient(app)  # a fresh set-up install with no password: open mode
    r = plain.post("/api/reading/model-check", files={"file": ("a.pdf", pdf(1), "application/pdf")},
                   data={"model": "qwen3.5:4b"})
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert check_state["current"] is None and tmp_files() == []


def test_refused_while_uploads_are_turned_off():
    settings_store.update("uploads", {"enabled": False})
    r = post()
    assert r.status_code == 403 and "Uploads are turned off" in r.json()["detail"]
    assert check_state["current"] is None and tmp_files() == []


def test_refused_while_a_document_is_being_read():
    state["reading"]["current"] = {"document_id": 1, "title": "x", "stage": "reader_a", "page": 1, "pages": 2}
    r = post()
    assert r.status_code == 409 and "being read" in r.json()["detail"]
    assert check_state["current"] is None and tmp_files() == []


def test_refused_while_another_check_runs(fakes):
    assert post().status_code == 202
    r = post()
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    run(fakes)  # the first one is untouched by the refusal
    assert check_state["last"]["status"] == "done"


def test_the_model_must_be_installed_and_from_ollamas_list(fakes):
    for bad in ("nope:1b", "../../etc/passwd", "http://evil.test/x", "", "qwen3.5:4b; rm -rf /"):
        r = post(model=bad)
        assert r.status_code == 422, bad
    assert not fakes and check_state["current"] is None and tmp_files() == []
    # "glm-ocr" is the installed "glm-ocr:latest": the name that is used comes from Ollama's list
    assert post(model="glm-ocr").status_code == 202
    assert fakes[0][1].model == "glm-ocr:latest"
    run(fakes)


def test_a_model_without_vision_is_refused_and_unknown_vision_is_allowed(fakes, monkeypatch):
    r = post(model="plain-text:1b")
    assert r.status_code == 422 and "images" in r.json()["detail"]
    assert not fakes and tmp_files() == [] and check_state["current"] is None
    monkeypatch.setattr(FakeOllama, "caps", {"qwen3.5:4b": None})  # an Ollama that does not report capabilities
    assert post().status_code == 202
    run(fakes)


def test_reader_b_must_be_installed_when_asked_for(fakes, monkeypatch):
    monkeypatch.setattr(FakeOllama, "installed", ["qwen3.5:4b"])
    r = post(use_reader_b="true")
    assert r.status_code == 422 and "glm-ocr" in r.json()["detail"]
    assert post().status_code == 202  # without reader B the same model is fine
    run(fakes)


def test_only_the_upload_types_are_accepted(fakes):
    r = post(file=b"just some text, not a document")
    assert r.status_code == 415 and tmp_files() == [] and check_state["current"] is None
    r = post(data={"use_reader_b": "maybe"})
    assert r.status_code == 422 and tmp_files() == []


def test_the_size_limit_of_uploads_applies(fakes):
    from app import settings_store

    settings_store.update("uploads", {"max_file_mb": 1})
    r = post(file=pdf(1) + b"0" * (1024 * 1024 + 10))
    assert r.status_code == 413 and tmp_files() == [] and check_state["current"] is None


# --- Ollama: the think fallback, capabilities, /api/ps -------------------------------------------------------

REAL_CLIENT = httpx.AsyncClient


def use_transport(monkeypatch, handler):
    monkeypatch.setattr(ollama.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def real() -> Ollama:
    return Ollama("http://ollama.test:11434", 30, 8192, "5m")


def test_think_is_retried_once_without_and_remembered(monkeypatch):
    monkeypatch.setattr(ollama, "_NO_THINK", set())
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append("think" in body)
        if "think" in body and body["model"] == "llama3.2-vision:11b":
            return httpx.Response(400, json={"error": '"llama3.2-vision:11b" does not support thinking'})
        return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps({"results": []})}})

    use_transport(monkeypatch, handler)
    assert asyncio.run(real().read_rows("llama3.2-vision:11b", b"x")) == []
    assert sent == [True, False]  # asked with think, then once without
    assert asyncio.run(real().read_rows("llama3.2-vision:11b", b"x")) == []
    assert sent == [True, False, False]  # remembered: not asked with think again
    assert asyncio.run(real().read_rows("qwen3.5:4b", b"x")) == []
    assert sent[-1] is True  # another model still gets think:false


def test_other_400s_are_not_retried(monkeypatch):
    monkeypatch.setattr(ollama, "_NO_THINK", set())
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "something else is wrong"})

    use_transport(monkeypatch, handler)
    with pytest.raises(OllamaError, match="Ollama error 400"):
        asyncio.run(real().read_rows("m", b"x"))
    assert len(calls) == 1


def test_a_reply_that_is_not_json_says_so(monkeypatch):
    use_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"done_reason": "stop", "message": {"content": "Sure! Here are the results: ..."}}))
    with pytest.raises(StructuredOutputError, match="structured output"):
        asyncio.run(real().read_rows("m", b"x"))
    use_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"done_reason": "stop", "message": {"content": '{"results": "none"}'}}))
    with pytest.raises(PageError, match="structured output"):
        asyncio.run(real().read_rows("m", b"x"))


def test_capabilities_and_gpu_percent(monkeypatch):
    def handler(request):
        if request.url.path == "/api/show":
            assert json.loads(request.content)["model"] == "qwen3.5:4b"
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"models": [
            {"name": "other:1b", "size": 100, "size_vram": 0},
            {"name": "qwen3.5:4b", "size": 4_000, "size_vram": 3_000},
        ]})

    use_transport(monkeypatch, handler)
    assert asyncio.run(real().capabilities("qwen3.5:4b")) == ["completion", "vision"]
    assert asyncio.run(real().gpu_percent("qwen3.5:4b")) == 75
    assert asyncio.run(real().gpu_percent("absent:1b")) is None

    use_transport(monkeypatch, lambda r: httpx.Response(200, json={"license": "x"}))  # an old Ollama: no capabilities
    assert asyncio.run(real().capabilities("qwen3.5:4b")) is None
    use_transport(monkeypatch, lambda r: httpx.Response(404, json={"error": "not found"}))
    assert asyncio.run(real().capabilities("qwen3.5:4b")) is None and asyncio.run(real().gpu_percent("x")) is None
