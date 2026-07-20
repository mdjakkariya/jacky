"""Unit tests for `jack mcp` (fake deps + captured output — no daemon)."""

from __future__ import annotations

import json
from typing import Any

from autobot.cli.mcp_cmd import run
from autobot.cli.mcp_repl import Deps


def _deps(**over: Any) -> Deps:
    base: dict[str, Any] = {
        "list_servers": lambda b: {"ok": True, "servers": []},
        "list_tools": lambda b, s: {"ok": True, "tools": []},
        "enable_server": lambda b, s: {"ok": True},
        "disable_server": lambda b, s: {"ok": True},
        "grant_consent": lambda b, s: {
            "ok": True,
            "server": {"server": s, "state": "connected", "tool_count": 1},
        },
        "remove_server": lambda b, s: {"ok": True},
        "add_server": lambda b, d: {"ok": True},
        "set_tool": lambda b, s, t, **kw: {"ok": True},
        "auth_start": lambda b, s: {"ok": True},
        "post_settings": lambda b, u: {"ok": True},
        "post_secret": lambda b, n, v: {"ok": True},
    }
    base.update(over)
    return Deps(**base)


def test_list_json_prints_payload() -> None:
    lines: list[str] = []
    payload = {"ok": True, "servers": [{"server": "s1"}]}
    rc = run(
        ["list", "--json"],
        base_url="http://b",
        deps=_deps(list_servers=lambda b: payload),
        out=lines.append,
    )
    assert rc == 0 and json.loads(lines[0]) == payload


def test_add_stdio_flags_build_descriptor() -> None:
    added: list[dict[str, Any]] = []

    def _add_server(b: str, d: dict[str, Any]) -> dict[str, Any]:
        added.append(d)
        return {"ok": True}

    rc = run(
        ["add", "pg", "--stdio", "--command", "npx", "--args", "-y", "srv", "--risk", "write"],
        base_url="http://b",
        deps=_deps(add_server=_add_server),
        out=lambda s: None,
    )
    d = added[0]
    assert rc == 0 and d["id"] == "pg" and d["command"] == "npx" and d["args"] == ["-y", "srv"]
    assert d["egress"] == "local" and d["enabled"] is False


def test_add_http_sets_url_and_network_egress() -> None:
    added: list[dict[str, Any]] = []

    def _add_server(b: str, d: dict[str, Any]) -> dict[str, Any]:
        added.append(d)
        return {"ok": True}

    rc = run(
        ["add", "slack", "--http", "--url", "https://mcp.slack.com/sse", "--auth", "oauth"],
        base_url="http://b",
        deps=_deps(add_server=_add_server),
        out=lambda s: None,
    )
    d = added[0]
    assert rc == 0 and d["url"] == "https://mcp.slack.com/sse"
    assert d["egress"] == "network" and d["auth"] == {"type": "oauth"}


def test_enable_pending_consent_asks_then_grants() -> None:
    granted: list[str] = []
    prompts: list[str] = []

    def _grant_consent(b: str, s: str) -> dict[str, Any]:
        granted.append(s)
        return {"ok": True, "server": {"state": "connected", "tool_count": 4}}

    def _ask(p: str) -> str:
        prompts.append(p)
        return "y"

    deps = _deps(
        enable_server=lambda b, s: {
            "ok": True,
            "pending_consent": True,
            "command": "npx",
            "args": ["-y", "srv"],
        },
        grant_consent=_grant_consent,
    )
    rc = run(["enable", "s1"], base_url="http://b", deps=deps, ask=_ask, out=lambda s: None)
    assert rc == 0 and granted == ["s1"] and "npx -y srv" in prompts[0]


def test_enable_yes_flag_skips_prompt() -> None:
    granted: list[str] = []

    def _grant_consent(b: str, s: str) -> dict[str, Any]:
        granted.append(s)
        return {"ok": True, "server": {}}

    def _no_ask(p: str) -> str:
        raise AssertionError("must not prompt")

    deps = _deps(
        enable_server=lambda b, s: {
            "ok": True,
            "pending_consent": True,
            "command": "npx",
            "args": [],
        },
        grant_consent=_grant_consent,
    )
    rc = run(
        ["enable", "s1", "--yes"], base_url="http://b", deps=deps, ask=_no_ask, out=lambda s: None
    )
    assert rc == 0 and granted == ["s1"]


def test_enable_denied_returns_nonzero_and_skips_grant() -> None:
    granted: list[str] = []

    def _grant_consent(b: str, s: str) -> dict[str, Any]:
        granted.append(s)
        return {"ok": True}

    deps = _deps(
        enable_server=lambda b, s: {
            "ok": True,
            "pending_consent": True,
            "command": "npx",
            "args": [],
        },
        grant_consent=_grant_consent,
    )
    rc = run(
        ["enable", "s1"], base_url="http://b", deps=deps, ask=lambda p: "n", out=lambda s: None
    )
    assert rc == 1 and granted == []


def test_auth_token_uses_getpass() -> None:
    stored: list[tuple[str, str]] = []

    def _post_secret(b: str, n: str, v: str) -> dict[str, Any]:
        stored.append((n, v))
        return {"ok": True}

    rc = run(
        ["auth", "notion", "--token"],
        base_url="http://b",
        deps=_deps(post_secret=_post_secret),
        ask_secret=lambda p: "tok-9",
        out=lambda s: None,
    )
    assert rc == 0 and stored == [("mcp.notion.token", "tok-9")]


def test_on_off_and_tool() -> None:
    updates: list[dict[str, Any]] = []
    tools: list[tuple[str, str, dict[str, Any]]] = []

    def _post_settings(b: str, u: dict[str, Any]) -> dict[str, Any]:
        updates.append(u)
        return {"ok": True}

    def _set_tool(b: str, s: str, t: str, **kw: Any) -> dict[str, Any]:
        tools.append((s, t, kw))
        return {"ok": True}

    deps = _deps(post_settings=_post_settings, set_tool=_set_tool)
    assert run(["off"], base_url="http://b", deps=deps, out=lambda s: None) == 0
    assert (
        run(
            ["tool", "gh", "rm", "--risk", "destructive"],
            base_url="http://b",
            deps=deps,
            out=lambda s: None,
        )
        == 0
    )
    assert updates == [{"allow_mcp": False}]
    assert tools == [("gh", "rm", {"risk": "destructive"})]


def test_unknown_verb_is_usage_error() -> None:
    assert run(["wat"], base_url="http://b", deps=_deps(), out=lambda s: None) == 2


def test_off_does_not_claim_success_on_failed_settings_write() -> None:
    lines: list[str] = []

    def _post_settings(b: str, u: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "disk full"}

    rc = run(
        ["off"],
        base_url="http://b",
        deps=_deps(post_settings=_post_settings),
        out=lines.append,
    )
    assert rc == 1
    assert not any("MCP disabled." in line for line in lines)
    assert any("disk full" in line for line in lines)


def test_auth_token_reports_failed_reconnect() -> None:
    lines: list[str] = []

    def _enable_server(b: str, s: str) -> dict[str, Any]:
        return {"ok": False, "error": "daemon unreachable"}

    rc = run(
        ["auth", "notion", "--token"],
        base_url="http://b",
        deps=_deps(enable_server=_enable_server),
        ask_secret=lambda p: "tok-9",
        out=lines.append,
    )
    assert rc == 1
    assert any("reconnect failed" in line for line in lines)
