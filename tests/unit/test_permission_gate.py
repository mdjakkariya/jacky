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


class _RecordingKindConfirmer:
    """Confirmer that approves and records the kind it was asked with."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.kinds.append(kind)
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default


def _gate(confirmer: object) -> tuple[ToolRegistry, PermissionGate]:
    reg = ToolRegistry()
    gate = PermissionGate(reg, AuditLog(":memory:"), confirmer)  # type: ignore[arg-type]
    return reg, gate


def test_network_write_tool_is_confirmed_with_network_kind() -> None:
    rec = _RecordingKindConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(
            name="slack__send",
            description="",
            parameters={},
            handler=lambda: "sent",
            risk=Risk.WRITE,
            network=True,
        )
    )
    result = gate.execute(ToolCall(name="slack__send", arguments={}))
    assert result.ok is True
    assert rec.kinds == ["network"]


def test_network_readonly_tool_is_not_confirmed() -> None:
    rec = _RecordingKindConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(
            name="slack__search",
            description="",
            parameters={},
            handler=lambda: "hits",
            risk=Risk.READ_ONLY,
            network=True,
        )
    )
    result = gate.execute(ToolCall(name="slack__search", arguments={}))
    assert result.ok is True
    assert rec.kinds == []  # network READ_ONLY: badge only, no card


def test_local_write_tool_is_not_confirmed() -> None:
    rec = _RecordingKindConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(
            name="local_write",
            description="",
            parameters={},
            handler=lambda: "ok",
            risk=Risk.WRITE,
            network=False,
        )
    )
    result = gate.execute(ToolCall(name="local_write", arguments={}))
    assert result.ok is True  # the tool actually ran (not blocked)
    assert rec.kinds == []  # local WRITE stays silent (unchanged behavior)


def test_destructive_tool_confirmed_with_danger_kind() -> None:
    rec = _RecordingKindConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(
            name="wipe",
            description="",
            parameters={},
            handler=lambda: "gone",
            risk=Risk.DESTRUCTIVE,
            network=False,
        )
    )
    result = gate.execute(ToolCall(name="wipe", arguments={}))
    assert result.ok is True  # confirmed (recorder approves) and ran
    assert rec.kinds == ["danger"]


def test_network_destructive_kind_is_network() -> None:
    rec = _RecordingKindConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(
            name="slack__delete",
            description="",
            parameters={},
            handler=lambda: "x",
            risk=Risk.DESTRUCTIVE,
            network=True,
        )
    )
    gate.execute(ToolCall(name="slack__delete", arguments={}))
    assert rec.kinds == ["network"]  # egress tint takes precedence


def test_builtin_confirmers_confirm_action() -> None:
    from autobot.tools.permission import AlwaysAllow, AlwaysDeny

    assert AlwaysAllow().confirm_action("go?") == "once"
    assert AlwaysDeny().confirm_action("go?") == ""


class _ScriptedConfirmer:
    """Returns queued confirm_action answers; records how many times it was asked."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.asks = 0

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.asks += 1
        return bool(self._answers)

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        self.asks += 1
        return self._answers.pop(0) if self._answers else ""


def _delete_gate(
    confirmer: object, scope_of: object | None = None
) -> tuple[PermissionGate, _SpyTool]:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="delete_file", description="", parameters={}, handler=tool, risk=Risk.DESTRUCTIVE
        )
    )
    gate = PermissionGate(
        registry,
        AuditLog(":memory:"),
        confirmer,  # type: ignore[arg-type]
        scope_of=scope_of,  # type: ignore[arg-type]
    )
    return gate, tool


def test_session_grant_skips_second_confirmation() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session"])
    gate, _tool = _delete_gate(confirmer, scope_of)
    r1 = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    r2 = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert r1.ok and r2.ok
    assert confirmer.asks == 1  # asked once; second was auto-approved


def test_once_does_not_remember() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["once", "once"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert confirmer.asks == 2  # asked every time


def test_session_grant_is_scoped_by_key() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session", "session"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/other/b"}))
    assert confirmer.asks == 2  # different scope -> asked again


def test_clear_session_grants_forgets() -> None:
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _ScriptedConfirmer(["session", "session"])
    gate, _ = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.clear_session_grants()
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert confirmer.asks == 2


def test_legacy_confirm_only_confirmer_still_works() -> None:
    # A confirmer with no confirm_action falls back to confirm(); never grants a session.
    gate, tool = _delete_gate(_RecordingConfirmer())  # confirm() -> False
    result = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert not tool.ran and not result.ok


def test_network_write_never_offers_session() -> None:
    tool = _SpyTool(Risk.WRITE)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="send", description="", parameters={}, handler=tool, risk=Risk.WRITE, network=True
        )
    )
    confirmer = _ScriptedConfirmer(
        ["session", "session"]
    )  # would grant if asked via confirm_action
    gate = PermissionGate(registry, AuditLog(":memory:"), confirmer, scope_of=lambda c: "x")  # type: ignore[arg-type]
    gate.execute(ToolCall(name="send", arguments={}))
    gate.execute(ToolCall(name="send", arguments={}))
    assert confirmer.asks == 2  # network path uses confirm() each time, no grant


class _LegacyApproveConfirmer:
    """A confirm()-only confirmer (no confirm_action) that always approves."""

    def __init__(self) -> None:
        self.asks = 0

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.asks += 1
        return True


def test_legacy_confirm_only_never_grants_session() -> None:
    # A confirmer with no confirm_action approves via confirm() every time and can
    # never be remembered for the session — so a batch still asks on each call.
    scope_of = lambda call: str(call.arguments.get("path", ""))  # noqa: E731
    confirmer = _LegacyApproveConfirmer()
    gate, tool = _delete_gate(confirmer, scope_of)
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert tool.ran is True
    assert confirmer.asks == 2  # legacy path never grants a session


def test_bogus_confirm_action_value_fails_closed() -> None:
    class _Bogus:
        def confirm(self, prompt: str, kind: str = "danger") -> bool:
            return True

        def confirm_action(self, prompt: str, kind: str = "danger") -> str:
            return "yes"  # not "once"/"session" -> must be treated as decline

    gate, tool = _delete_gate(_Bogus())
    result = gate.execute(ToolCall(name="delete_file", arguments={"path": "/d/a"}))
    assert tool.ran is False and result.ok is False  # unknown value fails closed


def test_pre_authorized_runs_destructive_without_confirming() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, audit = _gate_with(tool, "run_command", AlwaysDeny())  # would block if asked
    result = gate.execute(ToolCall(name="run_command", arguments={}), pre_authorized=True)
    assert tool.ran and result.ok  # ran despite AlwaysDeny — confirmer not consulted
    assert any(e.decision is Decision.ALLOWED for e in audit.recent(10))


def test_not_pre_authorized_still_confirms_destructive() -> None:
    tool = _SpyTool(Risk.DESTRUCTIVE)
    gate, _ = _gate_with(tool, "run_command", AlwaysDeny())
    result = gate.execute(ToolCall(name="run_command", arguments={}))
    assert not tool.ran and not result.ok  # default path unchanged: declined
