"""Subagents: the read-only executor, and the runner that spawns + delivers results."""

from __future__ import annotations

import time
from typing import Any

from autobot.agent.subagent import (
    _MAX_CONCURRENT_SUBAGENTS,
    _SUBAGENT_FRAMING,
    SubagentRunner,
    register_subagent_tool,
    subagent_executor,
)
from autobot.core.streaming import active_session_id
from autobot.core.types import Risk, ToolCall, ToolResult
from autobot.tasks import NotificationInbox, TaskRegistry
from autobot.tools.registry import ToolRegistry


class _Gate:
    """A minimal gate: risks come from a map; execute records + returns ok."""

    def __init__(self, risks: dict[str, Risk]) -> None:
        self._risks = risks
        self.executed: list[str] = []

    def risk_of(self, name: str) -> Risk | None:
        return self._risks.get(name)

    def execute(self, call: ToolCall, pre_authorized: bool = False) -> ToolResult:
        self.executed.append(call.name)
        return ToolResult(name=call.name, content="ran", ok=True)


class _FakeHarness:
    """Records the turn prompt; runs one write call through the executor to prove it's read-only."""

    def __init__(self, reply: str, prompts: list[str], executed: list[ToolResult]) -> None:
        self._reply = reply
        self._prompts = prompts
        self._executed = executed

    def run_turn(
        self, user_text: str, execute: Any, on_event: Any = None, should_cancel: Any = None
    ) -> str:
        self._prompts.append(user_text)
        self._executed.append(execute(ToolCall(name="edit_file", arguments={"path": "x"})))
        return self._reply


def _wait(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _runner(make: Any, gate: Any, reg: Any = None, inbox: Any = None) -> SubagentRunner:
    """Build a SubagentRunner from fakes (Any-typed so the structural fakes are accepted)."""
    return SubagentRunner(
        make,
        gate,
        reg if reg is not None else TaskRegistry(),
        inbox if inbox is not None else NotificationInbox(),
    )


# --- subagent_executor -------------------------------------------------------------------


def test_executor_refuses_spawn_agent() -> None:
    gate: Any = _Gate({"spawn_agent": Risk.READ_ONLY})
    res = subagent_executor(gate)(ToolCall(name="spawn_agent", arguments={"task": "x"}))
    assert res.ok is False and "can't spawn subagents" in res.content
    assert gate.executed == []  # never reached the gate


def test_executor_refuses_writes_and_commands() -> None:
    gate: Any = _Gate({"edit_file": Risk.WRITE, "run_command": Risk.DESTRUCTIVE})
    ex = subagent_executor(gate)
    assert ex(ToolCall(name="edit_file", arguments={})).ok is False
    assert ex(ToolCall(name="run_command", arguments={})).ok is False
    assert gate.executed == []


def test_executor_allows_read_only_tools() -> None:
    gate: Any = _Gate({"read_file": Risk.READ_ONLY})
    res = subagent_executor(gate)(ToolCall(name="read_file", arguments={"path": "a"}))
    assert res.ok is True and res.content == "ran"
    assert gate.executed == ["read_file"]


# --- SubagentRunner ----------------------------------------------------------------------


def test_spawn_registers_agent_task_and_delivers_result_to_parent() -> None:
    reg = TaskRegistry()
    inbox = NotificationInbox()
    gate: Any = _Gate({"edit_file": Risk.WRITE})
    prompts: list[str] = []
    executed: list[ToolResult] = []
    runner = _runner(
        lambda: _FakeHarness("SUMMARY: 3 call sites", prompts, executed), gate, reg, inbox
    )

    token = active_session_id.set("parent-session")
    try:
        ack = runner.spawn("find call sites of foo", "callsites")
    finally:
        active_session_id.reset(token)

    assert "task-1" in ack
    assert _wait(lambda: inbox.pending("parent-session") > 0), "result never delivered"
    note = inbox.drain("parent-session")[0]
    assert "task-1" in note and "SUMMARY: 3 call sites" in note

    row = reg.get("task-1")
    assert row is not None and row.kind == "agent" and row.status == "done"
    # The turn ran with the read-only framing + the task, and the executor refused the write.
    assert prompts[0].startswith(_SUBAGENT_FRAMING) and "find call sites of foo" in prompts[0]
    assert executed[0].ok is False  # edit_file was refused by subagent_executor


def test_spawn_requires_a_task() -> None:
    runner = _runner(lambda: _FakeHarness("x", [], []), _Gate({}))
    assert "what should the subagent do" in runner.spawn("   ").lower()


def test_concurrency_cap_refuses_beyond_the_limit() -> None:
    reg = TaskRegistry()
    inbox = NotificationInbox()
    # Fill the registry with the max number of *running* agent tasks.
    for i in range(_MAX_CONCURRENT_SUBAGENTS):
        reg.add(kind="agent", session_id="p", label=f"busy-{i}")
    runner = _runner(lambda: _FakeHarness("x", [], []), _Gate({}), reg, inbox)
    before = len(reg.list())
    ack = runner.spawn("one more task")
    assert "already running" in ack.lower()
    assert len(reg.list()) == before  # no new task registered


def test_spawn_failure_is_reported_not_raised() -> None:
    reg = TaskRegistry()
    inbox = NotificationInbox()

    def boom() -> Any:
        raise RuntimeError("model unavailable")

    runner = _runner(boom, _Gate({}), reg, inbox)
    token = active_session_id.set("p")
    try:
        runner.spawn("do research")
    finally:
        active_session_id.reset(token)
    assert _wait(lambda: inbox.pending("p") > 0), "failure never reported"
    assert "failed" in inbox.drain("p")[0].lower()
    assert reg.get("task-1").status == "failed"  # type: ignore[union-attr]


def test_register_subagent_tool_adds_spawn_agent() -> None:
    registry = ToolRegistry()
    runner = _runner(lambda: _FakeHarness("x", [], []), _Gate({}))
    register_subagent_tool(registry, runner)
    spec = registry.get("spawn_agent")
    assert spec is not None
    assert spec.risk == Risk.READ_ONLY and spec.core is True
