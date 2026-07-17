"""Unit tests for the CLI's MCP endpoint client (fake post/get/delete — no network)."""

from __future__ import annotations

import urllib.error
from typing import Any

from autobot.cli import mcp_client


def _recorder(result: dict[str, Any]) -> tuple[list[tuple[str, Any]], Any]:
    calls: list[tuple[str, Any]] = []

    def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append((url, payload))
        return result

    return calls, post


def test_list_servers_hits_endpoint() -> None:
    def get(url: str, timeout: float) -> dict[str, Any]:
        assert url == "http://b/mcp/servers"
        return {"ok": True, "servers": []}

    assert mcp_client.list_servers("http://b", get=get) == {"ok": True, "servers": []}


def test_list_servers_transport_error_is_soft() -> None:
    def get(url: str, timeout: float) -> dict[str, Any]:
        raise urllib.error.URLError("down")

    out = mcp_client.list_servers("http://b", get=get)
    assert out["ok"] is False and "down" in out["error"]


def test_enable_consent_disable_auth_urls() -> None:
    calls, post = _recorder({"ok": True})
    mcp_client.enable_server("http://b", "s1", post=post)
    mcp_client.grant_consent("http://b", "s1", post=post)
    mcp_client.disable_server("http://b", "s1", post=post)
    mcp_client.auth_start("http://b", "s1", post=post)
    assert [c[0] for c in calls] == [
        "http://b/mcp/servers/s1/enable",
        "http://b/mcp/servers/s1/consent",
        "http://b/mcp/servers/s1/disable",
        "http://b/mcp/servers/s1/auth/start",
    ]


def test_add_server_posts_descriptor() -> None:
    calls, post = _recorder({"ok": True})
    mcp_client.add_server("http://b", {"id": "s1", "transport": "stdio"}, post=post)
    assert calls == [("http://b/mcp/servers", {"id": "s1", "transport": "stdio"})]


def test_set_tool_sends_only_given_fields() -> None:
    calls, post = _recorder({"ok": True})
    mcp_client.set_tool("http://b", "s1", "t1", risk="destructive", post=post)
    mcp_client.set_tool("http://b", "s1", "t1", enabled=False, post=post)
    assert calls[0] == ("http://b/mcp/servers/s1/tools/t1", {"risk": "destructive"})
    assert calls[1] == ("http://b/mcp/servers/s1/tools/t1", {"enabled": False})


def test_remove_server_uses_delete() -> None:
    urls: list[str] = []

    def delete(url: str, timeout: float) -> dict[str, Any]:
        urls.append(url)
        return {"ok": True}

    assert mcp_client.remove_server("http://b", "s1", delete=delete) == {"ok": True}
    assert urls == ["http://b/mcp/servers/s1"]


def test_list_tools_hits_endpoint() -> None:
    def get(url: str, timeout: float) -> dict[str, Any]:
        assert url == "http://b/mcp/servers/s1/tools"
        return {"ok": True, "tools": [{"name": "t1"}]}

    result = mcp_client.list_tools("http://b", "s1", get=get)
    assert result == {"ok": True, "tools": [{"name": "t1"}]}


def test_list_tools_transport_error_is_soft() -> None:
    def get(url: str, timeout: float) -> dict[str, Any]:
        raise urllib.error.URLError("down")

    out = mcp_client.list_tools("http://b", "s1", get=get)
    assert out["ok"] is False and "down" in out["error"]


def test_remove_server_transport_error_is_soft() -> None:
    def delete(url: str, timeout: float) -> dict[str, Any]:
        raise urllib.error.URLError("gone")

    out = mcp_client.remove_server("http://b", "s1", delete=delete)
    assert out["ok"] is False and "gone" in out["error"]


def test_set_tool_sends_both_fields_when_given() -> None:
    calls, post = _recorder({"ok": True})
    mcp_client.set_tool("http://b", "s1", "t1", risk="write", enabled=True, post=post)
    assert calls == [("http://b/mcp/servers/s1/tools/t1", {"risk": "write", "enabled": True})]
