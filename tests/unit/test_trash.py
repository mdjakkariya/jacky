"""Tests for the empty_trash tool (no real Trash, no osascript)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry
from autobot.tools.trash import empty_trash, register_trash_tools


def test_empty_trash_runs_finder_and_reports_success() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    msg = empty_trash(runner=runner)
    assert "emptied the Trash" in msg
    # Always actually runs the empty (no unreliable pre-count short-circuit).
    assert calls and calls[0][0] == "osascript"


def test_empty_trash_reports_generic_failure() -> None:
    msg = empty_trash(runner=lambda _a: (1, "boom"))
    assert "couldn't empty" in msg.lower() and "boom" in msg


def test_empty_trash_explains_automation_permission_denied() -> None:
    denied = "Not allowed to send Apple events to Finder. (-1743)"
    msg = empty_trash(runner=lambda _a: (1, denied))
    assert "Automation" in msg and "Privacy" in msg


def test_registers_destructive_with_friendly_prompt() -> None:
    reg = ToolRegistry()
    register_trash_tools(reg)
    spec = reg.get("empty_trash")
    assert spec is not None
    assert spec.risk is Risk.DESTRUCTIVE
    assert spec.confirm_prompt and "Trash" in spec.confirm_prompt
