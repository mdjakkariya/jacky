"""Bounded act-phase auto-continue past a narrate-then-stop reply (issue 4 backstop)."""

from __future__ import annotations

from typing import Any

from autobot.agent.coder_turn import (
    MAX_ACT_CONTINUES,
    CoderTurnDriver,
    SuspendingConfirmer,
    _is_continuation_intent,
)
from autobot.config import Settings
from autobot.core.types import Risk
from autobot.tools.audit import AuditLog
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry, ToolSpec


def test_is_continuation_intent_fires_on_narration() -> None:
    for reply in (
        "The dev server is up. Let's run the tests now.",
        "Now I'll run the test suite.",
        "Next, I'll update the config.",
        "I'll now install the dependencies.",
    ):
        assert _is_continuation_intent(reply), reply


def test_is_continuation_intent_ignores_completion_reports() -> None:
    for reply in (
        "Done — 1 of 2 tests passed; the failing one expects the wrong heading.",
        "I've updated the file and the build is green.",
        "That command is blocked for safety.",
        "All set. Let me know if you want anything else.",  # closing, not a next step
    ):
        assert not _is_continuation_intent(reply), reply


def test_is_continuation_intent_ignores_long_summaries() -> None:
    # A substantial summary that merely ends with forward-looking words is NOT a stall —
    # re-running after it (the observed false-positive) burns turns.
    long_summary = (
        "Found your Playwright suite — 36 tests across example, google-search, home, and "
        "shopzone specs. I noticed a test-results folder with retries for T017, T015, T006, "
        "and the home page title test — let me actually run them now to see pass/fail status."
    )
    assert len(long_summary) > 200
    assert not _is_continuation_intent(long_summary)


class _ScriptedLLM:
    """Returns queued act replies in order (extra calls repeat the last), counting calls."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    def run_turn(
        self, user_text: str, execute: Any, on_event: Any = None, should_cancel: Any = None
    ) -> str:
        self.calls += 1
        idx = min(self.calls - 1, len(self._replies) - 1)
        return self._replies[idx]


def _confirm_driver(llm: _ScriptedLLM) -> CoderTurnDriver:
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
    settings = Settings(profile="coder", coding_autonomy="confirm")
    return CoderTurnDriver(llm, gate, sc, settings_provider=lambda: settings)


def test_act_auto_continues_then_stops() -> None:
    llm = _ScriptedLLM(["The server is up. Let's run the tests now.", "Done — all passed."])
    events = list(_confirm_driver(llm).start_stream("run the tests"))
    assert llm.calls == 2  # one continuation nudge, then it completed
    assert events[-1] == {"status": "done", "reply": "Done — all passed."}


def test_act_no_continue_on_completion() -> None:
    llm = _ScriptedLLM(["Done — all passed."])
    events = list(_confirm_driver(llm).start_stream("run the tests"))
    assert llm.calls == 1  # completion reply → no nudge
    assert events[-1]["reply"] == "Done — all passed."


def test_act_continue_is_capped() -> None:
    # A model that always narrates a next step is nudged at most MAX_ACT_CONTINUES times.
    llm = _ScriptedLLM(["Now I'll keep going."])
    list(_confirm_driver(llm).start_stream("go"))
    assert llm.calls == 1 + MAX_ACT_CONTINUES
