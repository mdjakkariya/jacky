"""Tests for the JSON-backed MCP server descriptors."""

from __future__ import annotations

from pathlib import Path

from autobot.mcp.config import (
    McpServerConfig,
    load_mcp_config,
    save_mcp_config,
)


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_mcp_config(tmp_path / "nope.json") == {}


def test_load_malformed_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_mcp_config(p) == {}


def test_load_parses_a_stdio_server(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text(
        """
        {"servers": {"slack": {
            "label": "Slack", "transport": "stdio", "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-slack"],
            "env": {"SLACK_TEAM_ID": "T0123"},
            "auth": {"type": "token"}, "token_env": "SLACK_BOT_TOKEN",
            "secret_ref": "mcp.slack.token", "enabled": false,
            "egress": "network", "default_risk": "write",
            "tool_allow": ["slack_*"], "tool_risk_overrides": {"slack_send_message": "write"}
        }}}
        """,
        encoding="utf-8",
    )
    servers = load_mcp_config(p)
    assert set(servers) == {"slack"}
    s = servers["slack"]
    assert s.id == "slack"
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ("-y", "@modelcontextprotocol/server-slack")
    assert s.env == {"SLACK_TEAM_ID": "T0123"}
    assert s.auth_type == "token"
    assert s.token_env == "SLACK_BOT_TOKEN"
    assert s.egress == "network"
    assert s.enabled is False
    assert s.tool_allow == ("slack_*",)
    assert s.tool_risk_overrides == {"slack_send_message": "write"}


def test_load_skips_server_with_bad_transport(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text('{"servers": {"x": {"transport": "carrier-pigeon"}}}', encoding="utf-8")
    assert load_mcp_config(p) == {}


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "mcp" / "servers.json"
    cfg = McpServerConfig(
        id="gh",
        label="GitHub",
        transport="http",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth2",
        secret_ref="mcp.gh.oauth",
        enabled=True,
        egress="network",
        default_risk="write",
        tool_allow=("repo_*",),
        tool_risk_overrides={"create_issue": "write"},
    )
    save_mcp_config({"gh": cfg}, p)
    assert p.exists()
    reloaded = load_mcp_config(p)
    assert reloaded == {"gh": cfg}


def test_save_sets_owner_only_perms(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    save_mcp_config({}, p)
    assert (p.stat().st_mode & 0o777) == 0o600
