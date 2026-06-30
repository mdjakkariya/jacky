"""End-to-end: McpManager connects to a real stdio MCP server, lists, calls, cleans up.

Skipped unless the optional `mcp` extra is installed. Run with:
    uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from autobot.mcp.config import McpServerConfig  # noqa: E402, RUF100
from autobot.mcp.manager import McpManager  # noqa: E402, RUF100
from autobot.tools.registry import ToolRegistry  # noqa: E402, RUF100

_SERVER = str(Path(__file__).parent / "echo_mcp_server.py")


def _wait(predicate: object, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.1)
    return False


def test_stdio_echo_connect_call_shutdown() -> None:
    cfg = McpServerConfig(
        id="echo",
        label="Echo",
        transport="stdio",
        command=sys.executable,
        args=(_SERVER,),
        enabled=True,
        egress="local",
    )
    registry = ToolRegistry()
    manager = McpManager({"echo": cfg}, registry)
    manager.start()
    try:
        manager.connect("echo")
        assert _wait(lambda: registry.get("echo__echo") is not None), "tool never registered"

        spec = registry.get("echo__echo")
        assert spec is not None
        assert spec.network is False  # local stdio server

        result = registry.dispatch("echo__echo", {"text": "hello"})
        assert result.ok is True
        assert "echo: hello" in result.content

        # all_tools() returns the full pre-filter snapshot including the whoami tool
        worker = manager._workers.get("echo")
        if worker is not None:
            snapshot = worker.all_tools()
            names = [t["name"] for t in snapshot]
            assert "echo" in names
            assert all(isinstance(t["enabled"], bool) for t in snapshot)
    finally:
        manager.shutdown(timeout=10.0)

    # Tools are unregistered when the server disconnects.
    assert registry.get("echo__echo") is None


def test_stdio_env_var_reaches_subprocess() -> None:
    """Prove that env vars (and by extension token injection) reach the subprocess."""
    cfg = McpServerConfig(
        id="echo",
        label="Echo",
        transport="stdio",
        command=sys.executable,
        args=(_SERVER,),
        enabled=True,
        egress="local",
        env={"ECHO_TOKEN": "sekret"},
        # auth_type="none" so the Keychain is NOT touched — just plain env passthrough
    )
    registry = ToolRegistry()
    manager = McpManager({"echo": cfg}, registry)
    manager.start()
    try:
        manager.connect("echo")
        assert _wait(lambda: registry.get("echo__whoami") is not None), "whoami never registered"

        result = registry.dispatch("echo__whoami", {})
        assert result.ok is True
        assert result.content == "sekret"
    finally:
        manager.shutdown(timeout=10.0)
