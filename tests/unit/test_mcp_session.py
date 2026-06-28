"""Unit tests for the pure parts of the MCP session worker (no SDK, no subprocess)."""

from __future__ import annotations

import asyncio
import concurrent.futures

from autobot.mcp.config import McpServerConfig
from autobot.mcp.session import McpServerWorker, _Call, tool_allowed
from autobot.tools.registry import ToolRegistry


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


def test_fail_pending_resolves_queued_calls_fast() -> None:
    # On worker exit, any still-queued call must be failed immediately so its caller
    # doesn't block for the full CALL_TIMEOUT_S.
    loop = asyncio.new_event_loop()
    try:
        cfg = McpServerConfig(id="s", label="s", transport="stdio")
        worker = McpServerWorker(cfg, ToolRegistry(), loop=loop)
        worker._queue = asyncio.Queue()
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        worker._queue.put_nowait(_Call(tool="t", args={}, future=future))
        worker._fail_pending()
        assert future.done()
        assert isinstance(future.exception(), RuntimeError)
    finally:
        loop.close()
