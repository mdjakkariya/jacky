"""Tests for the permission gate: confirmation policy + auditing.

These cover the Phase 1 acceptance criteria: a destructive action executes only
on confirmation, and every attempt leaves an audit entry.
"""

from __future__ import annotations

from autobot.core.types import Decision, Risk, ToolCall
from autobot.tools.audit import AuditLog
from autobot.tools.permission import AlwaysAllow, AlwaysDeny, PermissionGate
from autobot.tools.registry import ToolRegistry, ToolSpec


class _SpyTool:
    """A tool that records whether it actually ran."""

    def __init__(self, risk: Risk) -> None:
        self.ran = False
        self.risk = risk

    def __call__(self, **_kwargs: object) -> str:
        self.ran = True
        return "did the thing"


def _gate_with(tool: _SpyTool, name: str, confirmer: object) -> tuple[PermissionGate, AuditLog]:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name=name, description="", parameters={}, handler=tool, risk=tool.risk)
    )
    audit = AuditLog(":memory:")
    gate = PermissionGate(registry, audit, confirmer)  # type: ignore[arg-type]
    return gate, audit


class _RecordingConfirmer:
    """Captures the prompt it's asked to confirm; always declines."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.prompts.append(prompt)
        return False


class _TimeoutConfirmer:
    """Declines by timing out (no answer), like a confirmation that wasn't answered."""

    timed_out = True

    def confirm(self, _prompt: str, kind: str = "danger") -> bool:
        return False


def test_timed_out_confirmation_tells_model_no_confirmation_received() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, _ = _gate_with(tool, "empty_trash", _TimeoutConfirmer())
    result = gate.execute(ToolCall(name="empty_trash", arguments={}))
    assert not tool.ran
    assert not result.ok
    low = result.content.lower()
    assert "confirmation" in low and "cancel" in low  # not "the user declined"
    assert "declined" not in low


def test_explicit_decline_says_user_declined() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, _ = _gate_with(tool, "empty_trash", _RecordingConfirmer())  # timed_out absent -> False
    result = gate.execute(ToolCall(name="empty_trash", arguments={}))
    assert "declined" in result.content.lower()


def test_confirm_prompt_is_friendly_and_names_the_target() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    confirmer = _RecordingConfirmer()
    gate, _ = _gate_with(tool, "delete_file", confirmer)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "notes.txt"}))
    (prompt,) = confirmer.prompts
    assert "notes.txt" in prompt  # the actual target, not a generic line
    assert "delete_file" not in prompt and "DESTRUCTIVE" not in prompt  # no jargon
    assert "{" not in prompt  # never a raw args dict
    assert not tool.ran  # declined -> not executed


def test_generic_confirm_prompt_has_no_jargon() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    confirmer = _RecordingConfirmer()
    gate, _ = _gate_with(tool, "some_tool", confirmer)
    gate.execute(ToolCall(name="some_tool", arguments={"x": 1}))
    (prompt,) = confirmer.prompts
    assert "some_tool" not in prompt and "DESTRUCTIVE" not in prompt
    assert "x: 1" in prompt  # the argument is spelled out readably


def _gate_requiring(tool: _SpyTool, perm: str, status: str, opened: list[str]) -> PermissionGate:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="needs_perm",
            description="",
            parameters={},
            handler=tool,
            risk=tool.risk,
            requires=perm,
        )
    )
    return PermissionGate(
        registry,
        AuditLog(":memory:"),
        AlwaysAllow(),
        permission_status=lambda _k: status,
        on_permission_needed=opened.append,
    )


def test_tool_refused_when_required_permission_missing() -> None:
    tool = _SpyTool(Risk.WRITE)
    opened: list[str] = []
    gate = _gate_requiring(tool, "automation", "needed", opened)
    result = gate.execute(ToolCall(name="needs_perm", arguments={}))
    assert tool.ran is False  # never executed
    assert result.ok is False
    assert "permission" in result.content.lower()
    assert opened == ["automation"]  # opened the Settings pane


def test_tool_runs_when_permission_granted() -> None:
    tool = _SpyTool(Risk.WRITE)
    opened: list[str] = []
    gate = _gate_requiring(tool, "automation", "granted", opened)
    result = gate.execute(ToolCall(name="needs_perm", arguments={}))
    assert tool.ran is True and result.ok is True
    assert opened == []


def test_tool_runs_when_permission_unknown() -> None:
    # Unknown status must not block — the tool tries and we learn from the outcome.
    tool = _SpyTool(Risk.WRITE)
    gate = _gate_requiring(tool, "automation", "unknown", [])
    assert gate.execute(ToolCall(name="needs_perm", arguments={})).ok is True
    assert tool.ran is True


def test_risk_of_returns_tool_risk_or_none() -> None:
    tool = _SpyTool(Risk.WRITE)
    gate, _ = _gate_with(tool, "create_file", AlwaysAllow())
    assert gate.risk_of("create_file") is Risk.WRITE
    assert gate.risk_of("nonexistent_tool") is None


def test_write_runs_without_confirmation_and_is_audited() -> None:
    tool = _SpyTool(Risk.WRITE)
    # AlwaysDeny would block a confirmation; WRITE must not even ask.
    gate, audit = _gate_with(tool, "create_file", AlwaysDeny())
    result = gate.execute(ToolCall(name="create_file", arguments={"path": "a"}))
    assert tool.ran is True
    assert result.ok is True
    entry = audit.recent()[0]
    assert entry.decision is Decision.ALLOWED
    assert entry.risk == "WRITE"


def test_destructive_denied_does_not_run_and_is_audited() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, audit = _gate_with(tool, "delete_file", AlwaysDeny())
    result = gate.execute(ToolCall(name="delete_file", arguments={"path": "a"}))
    assert tool.ran is False
    assert result.ok is False
    assert "declined" in result.content
    entry = audit.recent()[0]
    assert entry.decision is Decision.DENIED
    assert entry.ok is None


def test_destructive_confirmed_runs_and_is_audited() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, audit = _gate_with(tool, "delete_file", AlwaysAllow())
    result = gate.execute(ToolCall(name="delete_file", arguments={"path": "a"}))
    assert tool.ran is True
    assert result.ok is True
    assert audit.recent()[0].decision is Decision.ALLOWED


def test_unknown_tool_is_denied_and_audited() -> None:
    audit = AuditLog(":memory:")
    gate = PermissionGate(ToolRegistry(), audit, AlwaysAllow())
    result = gate.execute(ToolCall(name="ghost", arguments={}))
    assert result.ok is False
    assert "unknown tool" in result.content
    assert audit.recent()[0].decision is Decision.DENIED
