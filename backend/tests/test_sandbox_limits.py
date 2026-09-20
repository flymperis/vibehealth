"""The limit-setting code of the child, run on every platform against a stand-in `resource` module (the
real one exists on POSIX only): which limits are set, to what, and that a refusal is tolerated."""

import sys
import types

import pytest

from app import sandbox_child


class FakeResource(types.ModuleType):
    RLIM_INFINITY = -1
    RLIMIT_CPU, RLIMIT_DATA, RLIMIT_CORE, RLIMIT_NOFILE, RLIMIT_AS = 0, 2, 4, 7, 9

    def __init__(self, current_hard=-1, refuse=()):
        super().__init__("resource")
        self.calls: dict[int, tuple[int, int]] = {}
        self.current_hard = current_hard
        self.refuse = set(refuse)

    def getrlimit(self, which):
        return (self.current_hard, self.current_hard)

    def setrlimit(self, which, pair):
        if which in self.refuse:
            raise ValueError("refused")
        self.calls[which] = pair


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        module = FakeResource(**kw)
        monkeypatch.setitem(sys.modules, "resource", module)
        return module

    return install


LIMITS = {"memory_bytes": 2 << 30, "memory_kind": "as", "cpu_seconds": 50, "open_files": 64}


def test_the_four_limits_are_set(fake):
    r = fake()
    applied = sandbox_child.apply_limits(LIMITS)
    assert r.calls == {
        FakeResource.RLIMIT_AS: (2 << 30, 2 << 30),
        FakeResource.RLIMIT_CPU: (50, 55),  # SIGXCPU at 50 s CPU, SIGKILL at 55
        FakeResource.RLIMIT_CORE: (0, 0),
        FakeResource.RLIMIT_NOFILE: (64, 64),
    }
    assert sorted(applied) == ["as", "core", "cpu", "nofile"]


def test_the_data_mode_limits_the_data_segment_instead(fake):
    r = fake()
    applied = sandbox_child.apply_limits({**LIMITS, "memory_kind": "data"})
    assert FakeResource.RLIMIT_DATA in r.calls and FakeResource.RLIMIT_AS not in r.calls
    assert "data" in applied and "as" not in applied


def test_no_memory_limit_when_none_is_asked_for(fake):
    r = fake()
    applied = sandbox_child.apply_limits({**LIMITS, "memory_bytes": None, "memory_kind": "none"})
    assert FakeResource.RLIMIT_AS not in r.calls and FakeResource.RLIMIT_DATA not in r.calls
    assert sorted(applied) == ["core", "cpu", "nofile"]


def test_a_limit_is_lowered_never_raised(fake):
    r = fake(current_hard=1 << 20)  # the launcher already capped everything at 1 MB / 1 M
    sandbox_child.apply_limits(LIMITS)
    assert r.calls[FakeResource.RLIMIT_AS] == (1 << 20, 1 << 20)
    assert r.calls[FakeResource.RLIMIT_CPU] == (50, 55)  # already below the ceiling


def test_a_limit_that_the_system_refuses_is_skipped_not_fatal(fake):
    r = fake(refuse=[FakeResource.RLIMIT_AS])
    applied = sandbox_child.apply_limits(LIMITS)
    assert "as" not in applied and {"cpu", "core", "nofile"} <= set(applied)
    assert FakeResource.RLIMIT_AS not in r.calls


def test_without_a_resource_module_nothing_is_applied(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)  # `import resource` raises ImportError
    assert sandbox_child.apply_limits(LIMITS) == []


def test_missing_values_are_not_guessed(fake):
    r = fake()
    applied = sandbox_child.apply_limits({})
    assert r.calls == {FakeResource.RLIMIT_CORE: (0, 0)} and applied == ["core"]
