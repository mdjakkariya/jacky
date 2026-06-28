"""Tests for the write-side system controls (helpers + each tool + dispatch)."""

from __future__ import annotations

from autobot.tools.toggles import (
    SystemToggles,
    clamp,
    first_int,
    is_accessibility_error,
)


class FakeRunner:
    """Records calls; returns one fixed (rc, out) for every command."""

    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.rc = rc
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.rc, self.out


class SeqRunner:
    """Returns queued (rc, out) responses in order; records calls."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self._responses.pop(0) if self._responses else (0, "")


class FakeProcs:
    """Fake ProcessManager: records started argvs and stopped pids."""

    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.stopped: list[int] = []
        self._next = 1000

    def start(self, argv: list[str]) -> int:
        self.started.append(argv)
        pid = self._next
        self._next += 1
        return pid

    def stop(self, pid: int) -> None:
        self.stopped.append(pid)


def test_clamp_bounds() -> None:
    assert clamp(130) == 100
    assert clamp(-5) == 0
    assert clamp(42) == 42


def test_first_int_extracts_number() -> None:
    assert first_int("30\n") == 30
    assert first_int("Wi-Fi Power (en0): On") == 0  # the 0 in en0
    assert first_int("nothing here") is None


def test_is_accessibility_error() -> None:
    assert is_accessibility_error("System Events got an error: ... (-1719)")
    assert is_accessibility_error("not allowed assistive access")
    assert not is_accessibility_error("some other error")


def test_constructs_with_fakes() -> None:
    tools = SystemToggles(FakeRunner(), FakeProcs())
    assert tools._awake_pid is None
