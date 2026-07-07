"""Orchestrator delegates coder-turn streaming calls to its driver (or errors when absent)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from autobot.orchestrator.state_machine import Orchestrator


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def start_stream(self, text: str) -> Iterator[dict[str, Any]]:
        self.calls.append(("start_stream", (text,)))
        yield {"status": "plan", "reply": "1. x", "todo": ["x"]}

    def reply_stream(self, value: str, text: str = "") -> Iterator[dict[str, Any]]:
        self.calls.append(("reply_stream", (value, text)))
        yield {"status": "done", "reply": "ok"}


def _bare_orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # no full build; exercise the two methods only
    orch.coder_driver = None
    return orch


def test_delegates_to_driver() -> None:
    orch = _bare_orchestrator()
    driver = _FakeDriver()
    orch.coder_driver = driver  # type: ignore[assignment]
    assert list(orch.start_coder_stream("do it"))[-1]["status"] == "plan"
    assert list(orch.reply_coder_stream("approve"))[-1]["status"] == "done"
    assert driver.calls == [("start_stream", ("do it",)), ("reply_stream", ("approve", ""))]


def test_start_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.start_coder_stream("do it"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]


def test_reply_coder_stream_no_driver_yields_error() -> None:
    orch = _bare_orchestrator()
    events = list(orch.reply_coder_stream("approve"))
    assert events == [{"status": "error", "reply": "coding turns aren't available here."}]
