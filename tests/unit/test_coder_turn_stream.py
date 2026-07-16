"""Driver streaming: start_stream/reply_stream yield interleaved tool + phase events."""

from __future__ import annotations

from typing import Any

from autobot.agent.coder_turn import CoderTurnDriver, SuspendingConfirmer
from autobot.config import Settings
from autobot.core.types import Risk
from autobot.tools.audit import AuditLog
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry, ToolSpec


class _EmittingLLM:
    """Plan turn returns a plan; act turn emits a tool event then returns the reply."""

    def __init__(self) -> None:
        self.plans = 0

    def run_turn(
        self, user_text: str, execute: Any, on_event: Any = None, should_cancel: Any = None
    ) -> str:
        if "PLANNING" in user_text:
            self.plans += 1
            return "1. run the script"
        if on_event is not None:
            on_event({"type": "tool", "event": "start", "name": "run_command", "label": "$ ./x"})
            on_event(
                {
                    "type": "tool",
                    "event": "end",
                    "name": "run_command",
                    "label": "$ ./x",
                    "ok": True,
                }
            )
        return "Ran it."


def _driver(llm: _EmittingLLM) -> CoderTurnDriver:
    sc = SuspendingConfirmer()
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="run_command",
            description="",
            parameters={},
            handler=lambda **_k: "out",
            risk=Risk.DESTRUCTIVE,
        )
    )
    gate = PermissionGate(reg, AuditLog(":memory:"), sc)
    settings = Settings(profile="coder", coding_autonomy="plan")
    return CoderTurnDriver(llm, gate, sc, settings_provider=lambda: settings)


def test_start_stream_yields_plan_then_stops() -> None:
    d = _driver(_EmittingLLM())
    events = list(d.start_stream("run the script"))
    assert events[-1]["status"] == "plan"  # stream stops at the suspend event
    assert all(e.get("status") != "done" for e in events)
    # Resume to completion: a turn parked at "plan" leaves its worker thread (named
    # "coder-turn") blocked forever if never answered. Other suites' tests scan for
    # live threads by that name, so a test-isolation leak here would fail them, not us.
    list(d.reply_stream("approve"))


def test_reply_stream_yields_tool_events_then_done() -> None:
    d = _driver(_EmittingLLM())
    list(d.start_stream("run the script"))  # park at plan
    events = list(d.reply_stream("approve"))
    tools = [e for e in events if e.get("type") == "tool"]
    assert any(e["event"] == "start" for e in tools)
    assert events[-1] == {"status": "done", "reply": "Ran it."}
