"""Tests for the update_plan tool + its registration."""

from __future__ import annotations

from typing import Any

from autobot.core.streaming import plan_sink
from autobot.core.types import Risk
from autobot.tools.code.plan import register_plan_tool, update_plan
from autobot.tools.registry import ToolRegistry


def test_update_plan_calls_the_sink_and_acks() -> None:
    seen: list[list[dict[str, Any]]] = []
    token = plan_sink.set(seen.append)
    try:
        ack = update_plan([{"step": "a", "status": "done"}, {"step": "b", "status": "pending"}])
    finally:
        plan_sink.reset(token)
    assert seen == [[{"step": "a", "status": "done"}, {"step": "b", "status": "pending"}]]
    assert "1/2 done" in ack


def test_update_plan_no_sink_is_a_noop_ack() -> None:
    assert update_plan([{"step": "a", "status": "done"}])  # returns a string, no raise
    assert update_plan(None) == "No plan steps given."


def test_register_plan_tool_registers_core_readonly() -> None:
    reg = ToolRegistry()
    register_plan_tool(reg)
    spec = next(s for s in reg.specs() if s.name == "update_plan")
    assert spec.core is True and spec.risk == Risk.READ_ONLY
