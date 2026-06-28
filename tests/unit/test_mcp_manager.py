"""Lifecycle tests for McpManager — no live server, no SDK required."""

from __future__ import annotations

import time

from autobot.mcp.config import McpServerConfig
from autobot.mcp.manager import McpManager
from autobot.tools.registry import ToolRegistry


def _cfg(server_id: str, *, enabled: bool) -> McpServerConfig:
    # A command that cannot start a real MCP server, so connect() degrades to "error"
    # rather than registering tools — exercises the graceful-degradation path.
    return McpServerConfig(
        id=server_id,
        label=server_id,
        transport="stdio",
        command="this-command-does-not-exist-xyz",
        args=(),
        enabled=enabled,
    )


def test_start_then_shutdown_is_clean() -> None:
    mgr = McpManager({}, ToolRegistry())
    mgr.start()
    mgr.shutdown(timeout=5.0)  # must return without hanging


def test_shutdown_without_start_is_noop() -> None:
    McpManager({}, ToolRegistry()).shutdown()  # no exception


def test_connect_bad_server_degrades_to_error_without_crashing() -> None:
    reg = ToolRegistry()
    mgr = McpManager({"bad": _cfg("bad", enabled=True)}, reg)
    mgr.start()
    try:
        mgr.connect("bad")
        deadline = time.time() + 8.0
        while time.time() < deadline:
            states = {s["server"]: s["state"] for s in mgr.status()}
            if states.get("bad") in {"error", "disconnected"}:
                break
            time.sleep(0.05)
        states = {s["server"]: s["state"] for s in mgr.status()}
        assert states.get("bad") in {"error", "disconnected"}
        assert reg.get("bad__anything") is None  # no tools registered from a dead server
    finally:
        mgr.shutdown(timeout=5.0)


def test_status_lists_all_configured_servers() -> None:
    mgr = McpManager({"a": _cfg("a", enabled=False), "b": _cfg("b", enabled=False)}, ToolRegistry())
    ids = {s["server"] for s in mgr.status()}
    assert ids == {"a", "b"}


def test_start_shutdown_restart_cycle() -> None:
    # shutdown() closes the loop; a later start() must build a fresh one and run again,
    # so a reloadable manager can stop and restart without leaking or erroring.
    mgr = McpManager({}, ToolRegistry())
    mgr.start()
    mgr.shutdown(timeout=5.0)
    mgr.start()  # fresh loop after the previous one was closed
    mgr.shutdown(timeout=5.0)  # must not raise
