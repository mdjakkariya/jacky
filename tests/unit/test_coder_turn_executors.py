"""read_only_executor blocks writes; act_executor routes run_command by policy."""

from __future__ import annotations

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
    ex(ToolCall(name="run_command", arguments={"command": "npm run build"}))
    assert gate.calls == [("run_command", False)]  # falls through → gate confirms (asks CLI)


def test_act_auto_mode_runs_confirm_command_without_asking() -> None:
    gate = _FakeGate({"run_command": Risk.DESTRUCTIVE})
    ex = act_executor(gate, allowlist=[], blocklist=[], ask_on_confirm=False)  # type: ignore[arg-type]
    ex(ToolCall(name="run_command", arguments={"command": "npm run build"}))
    assert gate.calls == [("run_command", True)]  # auto: pre_authorized, no ask


def test_act_passes_edits_straight_through() -> None:
    gate = _FakeGate({"edit_file": Risk.WRITE})
    ex = act_executor(gate, allowlist=[], blocklist=[])  # type: ignore[arg-type]
    ex(ToolCall(name="edit_file", arguments={"path": "x"}))
    assert gate.calls == [("edit_file", False)]  # WRITE < threshold → gate won't confirm
