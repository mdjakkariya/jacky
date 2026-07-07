"""Orchestrator delegates coder-turn calls to its driver (or errors when absent)."""

from __future__ import annotations

from typing import Any

from autobot.orchestrator.state_machine import Orchestrator


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def start(self, text: str) -> dict[str, Any]:
        self.calls.append(("start", (text,)))
        return {"status": "plan", "reply": "1. x", "todo": ["x"]}

    def reply(self, value: str, text: str = "") -> dict[str, Any]:
        self.calls.append(("reply", (value, text)))
        return {"status": "done", "reply": "ok"}


def _bare_orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # no full build; exercise the two methods only
    orch.coder_driver = None
    return orch


def test_delegates_to_driver() -> None:
    orch = _bare_orchestrator()
    driver = _FakeDriver()
    orch.coder_driver = driver  # type: ignore[assignment]
    assert orch.start_coder_turn("do it")["status"] == "plan"
    assert orch.reply_coder_turn("approve")["status"] == "done"
    assert driver.calls == [("start", ("do it",)), ("reply", ("approve", ""))]


def test_errors_without_driver() -> None:
    orch = _bare_orchestrator()
    assert orch.start_coder_turn("x")["status"] == "error"
    assert orch.reply_coder_turn("yes")["status"] == "error"


def test_start_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.start_coder_stream("do it"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]


def test_reply_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.reply_coder_stream("approve"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]
