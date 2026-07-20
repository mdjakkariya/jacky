"""read_only_executor blocks writes; act_executor routes run_command by policy."""

from __future__ import annotations

import logging

import pytest

from autobot.agent.coder_turn import act_executor, read_only_executor
from autobot.core.types import Risk, ToolCall, ToolResult


class _FakeGate:
    """Records execute() calls and reports canned risks."""

    def __init__(self, risks: dict[str, Risk]) -> None:
        self._risks = risks
        self.calls: list[tuple[str, bool]] = []  # (name, pre_authorized)

    def risk_of(self, name: str) -> Risk | None:
        return self._risks.get(name)

    def execute(self, call: ToolCall, *, pre_authorized: bool = False) -> ToolResult:
        self.calls.append((call.name, pre_authorized))
        return ToolResult(name=call.name, content="ran", ok=True)


def test_read_only_refuses_writes_runs_reads() -> None:
    gate = _FakeGate({"read_file": Risk.READ_ONLY, "edit_file": Risk.WRITE})
    ex = read_only_executor(gate)  # type: ignore[arg-type]

    write = ex(ToolCall(name="edit_file", arguments={"path": "x"}))
    assert not write.ok and "plan" in write.content.lower()
    assert gate.calls == []  # never dispatched

    read = ex(ToolCall(name="read_file", arguments={"path": "x"}))
    assert read.ok and gate.calls == [("read_file", False)]


def test_act_blocks_dangerous_command() -> None:
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[])  # type: ignore[arg-type]
    res = ex(ToolCall(name="run_command", arguments={"command": "rm -rf /"}))
    assert not res.ok and "blocked" in res.content.lower()
    assert gate.calls == []  # never reached the gate


def test_act_allowlisted_runs_pre_authorized() -> None:
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=["pytest*"], blocklist=[])  # type: ignore[arg-type]
    ex(ToolCall(name="run_command", arguments={"command": "pytest -q"}))
    assert gate.calls == [("run_command", True)]  # pre_authorized → no ask


def test_act_confirm_command_asks_gate() -> None:
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[])  # type: ignore[arg-type]
    # npm install is not read-only, so it falls through to the gate (confirm).
    ex(ToolCall(name="run_command", arguments={"command": "npm install"}))
    assert gate.calls == [("run_command", False)]  # falls through → gate confirms (asks CLI)


def test_act_auto_mode_runs_confirm_command_without_asking() -> None:
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[], ask_on_confirm=False)  # type: ignore[arg-type]
    ex(ToolCall(name="run_command", arguments={"command": "npm install"}))
    assert gate.calls == [("run_command", True)]  # auto: pre_authorized, no ask


def test_read_only_command_does_not_snapshot() -> None:
    fired: list[str] = []
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(
        gate,  # type: ignore[arg-type]
        allowlist=[],
        blocklist=[],
        before_mutation=lambda: fired.append("x"),
    )
    ex(ToolCall(name="run_command", arguments={"command": "git status"}))
    assert fired == []  # read-only: no checkpoint taken
    assert gate.calls == [("run_command", True)]  # read-only → auto-run pre-authorized


def test_writing_command_snapshots() -> None:
    fired: list[str] = []
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(
        gate,  # type: ignore[arg-type]
        allowlist=[],
        blocklist=[],
        before_mutation=lambda: fired.append("x"),
    )
    ex(ToolCall(name="run_command", arguments={"command": "npm install"}))
    assert fired == ["x"]  # mutating command: checkpoint taken first


def test_act_passes_edits_straight_through() -> None:
    gate = _FakeGate({"edit_file": Risk.WRITE})
    ex = act_executor(gate, allowlist=[], blocklist=[])  # type: ignore[arg-type]
    ex(ToolCall(name="edit_file", arguments={"path": "x"}))
    assert gate.calls == [("edit_file", False)]  # WRITE < threshold → gate won't confirm


def test_act_destructive_file_op_pre_authorized_under_approved_plan() -> None:
    # An approved plan (ask_on_confirm=False) authorizes its delete/move — run pre-authorized.
    gate = _FakeGate({"delete_file": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[], ask_on_confirm=False)  # type: ignore[arg-type]
    ex(ToolCall(name="delete_file", arguments={"path": "x"}))
    assert gate.calls == [("delete_file", True)]  # pre_authorized → no second prompt


def test_act_destructive_file_op_asks_in_confirm_mode() -> None:
    gate = _FakeGate({"move_file": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[], ask_on_confirm=True)  # type: ignore[arg-type]
    ex(ToolCall(name="move_file", arguments={"source": "a", "dest": "b"}))
    assert gate.calls == [("move_file", False)]  # confirm mode → gate asks


def test_act_caps_logged_command_length(caplog: pytest.LogCaptureFixture) -> None:
    # A long/newline-laden command must not bloat the debug log (logs are "signal, not
    # noise") — the logged cmd= value is capped even though classify_command still sees
    # the full command (the allowlist match, not the log line, decides the outcome).
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=["pytest*"], blocklist=[])  # type: ignore[arg-type]
    long_command = "pytest " + ("x" * 500)

    with caplog.at_level(logging.INFO, logger="autobot.coder"):
        ex(ToolCall(name="run_command", arguments={"command": long_command}))

    assert gate.calls == [("run_command", True)]  # still matched the allowlist in full
    messages = [r.getMessage() for r in caplog.records]
    assert any("command auto-run" in m for m in messages)
    for message in messages:
        assert len(message) < 300  # the raw 500+ char command never reaches the log
