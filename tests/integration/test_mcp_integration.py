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
    finally:
        manager.shutdown(timeout=10.0)

    # Tools are unregistered when the server disconnects.
    assert registry.get("echo__echo") is None
