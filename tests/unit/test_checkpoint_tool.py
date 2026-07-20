"""Tests for the model-facing undo tool (delegates to the injected restore closure)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.code.checkpoint_tool import register_undo_tool
from autobot.tools.registry import ToolRegistry


def test_undo_tool_returns_restore_message() -> None:
    reg = ToolRegistry()
    register_undo_tool(reg, lambda: (True, "restored to refs/jack/checkpoints/2"))
    res = reg.dispatch("undo", {})
    assert res.ok
    assert "restored to" in res.content


def test_undo_tool_reports_nothing_to_undo() -> None:
    reg = ToolRegistry()
    register_undo_tool(reg, lambda: (False, "Nothing to undo."))
    assert "nothing to undo" in reg.dispatch("undo", {}).content.lower()


def test_undo_tool_is_destructive() -> None:
    reg = ToolRegistry()
    register_undo_tool(reg, lambda: (True, "ok"))
    spec = reg.get("undo")
    assert spec is not None
    assert spec.risk == Risk.DESTRUCTIVE  # reverting files → confirmed in confirm mode
