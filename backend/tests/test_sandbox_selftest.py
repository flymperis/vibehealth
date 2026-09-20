"""The start-up check that the sandbox child can work under its limits, and the memory-limit ladder
(address space -> data segment -> none) that keeps a limit from making every upload fail."""

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from app import sandbox
from app.main import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name == "posix"


def limited_child(failing_modes):
    """A child that crashes when the server asks for one of `failing_modes` and otherwise says the file is fine."""
    code = (
        f"import sys; sys.path.insert(0, {ROOT!r})\n"
        "import json, os, struct\n"
        "req = json.loads(sys.stdin.readline())\n"
        f"if req['limits']['memory_kind'] in {list(failing_modes)!r}: os._exit(9)\n"
        "m = json.dumps({'ok': True, 'pages': 1}).encode(); p = b'\\xff\\xd8\\xff\\xe0x'\n"
        "os.write(1, b'VHSB1' + struct.pack('>II', len(m), len(p)) + m + p)"
    )
    return [sys.executable, "-c", code]


@pytest.fixture(autouse=True)
def keep_mode(monkeypatch):
    monkeypatch.setattr(sandbox, "MEMORY_MODE", "as")
    monkeypatch.setattr(sandbox, "MEMORY_MB", 2048)


def test_the_real_child_passes_its_own_self_test(caplog):
    with caplog.at_level("INFO", logger="vibehealth"):
        assert sandbox.selftest() is True
    assert sandbox.MEMORY_MODE == "as" and "self-test passed" in caplog.text


def test_the_tiny_files_of_the_self_test_are_valid(tmp_path):
    (tmp_path / "a.pdf").write_bytes(sandbox._tiny_pdf())
    (tmp_path / "a.png").write_bytes(sandbox._tiny_png())
    assert sandbox.run("check_upload", {"path": str(tmp_path / "a.pdf"), "kind": "pdf"}).meta["pages"] == 1
    assert sandbox.run("check_upload", {"path": str(tmp_path / "a.png"), "kind": "png"}).meta["pages"] == 1


def test_a_child_that_cannot_run_under_the_address_space_limit_steps_down_to_the_data_limit(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "_child_argv", lambda: limited_child(["as"]))
    with caplog.at_level("INFO", logger="vibehealth"):
        assert sandbox.selftest() is True
    assert sandbox.MEMORY_MODE == "data"
    assert "could not run with the memory limit 'as'" in caplog.text and "now runs with 'data'" in caplog.text
    assert sandbox._limits(10)["memory_kind"] == "data" and sandbox._limits(10)["memory_bytes"] == 2048 * 1024 * 1024


def test_and_from_there_to_no_memory_limit(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "_child_argv", lambda: limited_child(["as", "data"]))
    with caplog.at_level("INFO", logger="vibehealth"):
        assert sandbox.selftest() is True
    assert sandbox.MEMORY_MODE == "none" and sandbox._limits(10)["memory_bytes"] is None
    assert "now runs with 'none'" in caplog.text


def test_a_sandbox_that_cannot_start_at_all_is_reported_loudly_and_the_mode_is_kept(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "_child_argv", lambda: limited_child(["as", "data", "none"]))
    with caplog.at_level("ERROR", logger="vibehealth"):
        assert sandbox.selftest() is False
    assert sandbox.MEMORY_MODE == "as" and "self-test FAILED" in caplog.text


def test_a_zero_memory_setting_means_no_memory_limit(monkeypatch):
    monkeypatch.setattr(sandbox, "MEMORY_MB", 0)
    assert sandbox._limits(10)["memory_bytes"] is None
    assert sandbox.selftest() is True and sandbox.MEMORY_MODE == "none"  # (nothing to step down from)


@pytest.mark.skipif(not POSIX, reason="resource limits exist on POSIX only")
def test_the_data_limit_is_what_the_child_gets_in_that_mode(monkeypatch):
    monkeypatch.setattr(sandbox, "MEMORY_MODE", "data")
    monkeypatch.setattr(sandbox, "MEMORY_MB", 700)
    body = (
        "def handler(a):\n"
        "    import resource, json\n"
        "    return {'ok': True}, json.dumps({'data': resource.getrlimit(resource.RLIMIT_DATA)[0]}).encode()"
    )
    argv = [sys.executable, "-c",
            f"import sys; sys.path.insert(0, {ROOT!r})\nfrom app import sandbox_child as c\n{body}\n"
            "c.COMMANDS['probe'] = handler\nc.main()"]
    monkeypatch.setattr(sandbox, "_child_argv", lambda: argv)
    assert json.loads(sandbox.run("probe", {}).payload)["data"] == 700 * 1024 * 1024
    assert "data" in sandbox.run("probe", {}).limits


def test_the_app_runs_the_self_test_at_start_and_starts_even_if_it_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "selftest", lambda: calls.append(1) or True)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
    assert calls == [1]

    def broken():
        raise RuntimeError("the self-test itself is broken")

    monkeypatch.setattr(sandbox, "selftest", broken)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
