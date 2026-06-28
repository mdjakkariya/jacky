"""Unit tests for the pure parts of the MCP session worker (no SDK, no subprocess)."""

from __future__ import annotations

from autobot.mcp.session import tool_allowed


def test_tool_allowed_empty_allow_permits_all() -> None:
    assert tool_allowed("anything", (), ()) is True


def test_tool_allowed_allow_glob_filters() -> None:
    assert tool_allowed("slack_send", ("slack_*",), ()) is True
    assert tool_allowed("github_pr", ("slack_*",), ()) is False


def test_tool_allowed_deny_glob_wins_over_allow() -> None:
    assert tool_allowed("slack_admin_delete", ("slack_*",), ("*_delete",)) is False


def test_tool_allowed_deny_without_allow() -> None:
    assert tool_allowed("dangerous", (), ("dang*",)) is False
    assert tool_allowed("safe", (), ("dang*",)) is True
