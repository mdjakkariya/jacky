"""Unit tests for the MCP table/line renderers (pure — rendered to plain text)."""

from __future__ import annotations

from io import StringIO
from typing import Any

from rich.console import Console

from autobot.cli import mcp_render


def _plain(renderable: Any) -> str:
    buf = StringIO()
    Console(file=buf, width=120, force_terminal=False).print(renderable)
    return buf.getvalue()


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "server": "github",
        "label": "GitHub",
        "enabled": True,
        "egress": "local",
        "auth_type": "none",
        "state": "connected",
        "tool_count": 12,
        "secret_present": False,
        "transport": "stdio",
    }
    row.update(over)
    return row


def test_state_label_derivations() -> None:
    assert mcp_render.state_label(_row())[0] == "● connected"
    assert mcp_render.state_label(_row(state="pending_consent"))[0] == "◌ pending consent"
    needs_auth = _row(state="disconnected", auth_type="oauth", secret_present=False)
    assert mcp_render.state_label(needs_auth)[0] == "○ auth needed"
    assert mcp_render.state_label(_row(state="error"))[0] == "✕ error"


def test_render_servers_table_and_empty() -> None:
    out = _plain(mcp_render.render_servers({"ok": True, "servers": [_row()]}))
    assert "github" in out and "connected" in out and "12" in out and "stdio" in out
    empty = mcp_render.render_servers({"ok": True, "servers": []})
    assert "No MCP servers" in str(empty)


def test_render_servers_disabled_message() -> None:
    out = mcp_render.render_servers({"ok": False, "error": "mcp disabled"})
    assert "/mcp on" in str(out)


def test_render_tools_marks_reconsent() -> None:
    payload = {
        "ok": True,
        "tools": [
            {
                "name": "list_issues",
                "risk": "read_only",
                "enabled": True,
                "pending_reconsent": False,
                "network": False,
                "description": "",
            },
            {
                "name": "create_repo",
                "risk": "write",
                "enabled": True,
                "pending_reconsent": True,
                "network": False,
                "description": "",
            },
        ],
    }
    out = _plain(mcp_render.render_tools("github", payload))
    assert "re-consent" in out and "list_issues" in out


def test_render_mcp_event_lines() -> None:
    line = mcp_render.render_mcp_event(
        {"type": "mcp_status", "server": "s1", "state": "connected", "tool_count": 3}
    )
    assert "s1" in str(line) and "3" in str(line)
    assert (
        mcp_render.render_mcp_event(
            {"type": "mcp_status", "server": "s1", "state": "disconnected", "tool_count": 0}
        )
        is None
    )
    oauth = mcp_render.render_mcp_event(
        {"type": "mcp_oauth", "server": "s1", "stage": "browser_open"}
    )
    assert "browser" in str(oauth)
