"""Lifecycle tests for McpManager — no live server, no SDK required."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

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


def _write_servers_json(tmp_path: Path, servers: dict[str, Any]) -> Path:
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return p


def test_add_or_update_server_persists_new_server(tmp_path: Path) -> None:
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)

    result = mgr.add_or_update_server(
        {
            "id": "echo",
            "label": "Echo",
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "echo_server"],
            "enabled": False,
        }
    )

    assert result["ok"] is True
    saved = json.loads(p.read_text())
    assert "echo" in saved["servers"]


def test_add_or_update_server_rejects_invalid_transport(tmp_path: Path) -> None:
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)

    result = mgr.add_or_update_server({"id": "bad", "transport": "grpc"})
    assert result["ok"] is False
    assert "error" in result


def test_remove_server_removes_from_config_and_disk(tmp_path: Path) -> None:
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    removed = mgr.remove_server("echo")

    assert removed is True
    saved = json.loads(p.read_text())
    assert "echo" not in saved["servers"]
    assert mgr.remove_server("echo") is False  # idempotent: second call returns False


def test_set_enabled_persists_flag(tmp_path: Path) -> None:
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    ok = mgr.set_enabled("echo", True)

    assert ok is True
    saved = json.loads(p.read_text())
    assert saved["servers"]["echo"]["enabled"] is True


def test_set_enabled_returns_false_for_unknown(tmp_path: Path) -> None:
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)
    assert mgr.set_enabled("nonexistent", True) is False


def test_set_tool_override_deny_persists(tmp_path: Path) -> None:
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    ok = mgr.set_tool_override("echo", "echo__dangerous", enabled=False)

    assert ok is True
    saved = json.loads(p.read_text())
    assert "echo__dangerous" in saved["servers"]["echo"]["tool_deny"]


def test_set_tool_override_risk_persists(tmp_path: Path) -> None:
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    ok = mgr.set_tool_override("echo", "echo__read", risk="read_only")

    assert ok is True
    saved = json.loads(p.read_text())
    assert saved["servers"]["echo"]["tool_risk_overrides"]["echo__read"] == "read_only"


def test_secret_present_returns_false_when_no_secret_ref(tmp_path: Path) -> None:
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)
    assert mgr.secret_present("echo") is False


def test_set_tool_override_deny_is_idempotent(tmp_path: Path) -> None:
    # Disabling the same tool twice must not duplicate it in tool_deny.
    servers = {"echo": {"transport": "stdio", "command": "python", "enabled": False}}
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config

    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)
    mgr.set_tool_override("echo", "echo__danger", enabled=False)
    mgr.set_tool_override("echo", "echo__danger", enabled=False)
    saved = json.loads(p.read_text())
    assert saved["servers"]["echo"]["tool_deny"].count("echo__danger") == 1


def test_add_or_update_server_rejects_missing_id(tmp_path: Path) -> None:
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)
    result = mgr.add_or_update_server({"transport": "stdio", "command": "python"})
    assert result["ok"] is False
    assert "id" in result["error"]


def _http_cfg(
    server_id: str,
    *,
    auth_type: str = "oauth",
    enabled: bool = False,
) -> McpServerConfig:
    """An HTTP server config for start_oauth tests."""
    return McpServerConfig(
        id=server_id,
        label=server_id,
        transport="http",
        url="http://localhost:9999",
        auth_type=auth_type,
        enabled=enabled,
    )


def test_start_oauth_unknown_server_returns_error() -> None:
    mgr = McpManager({}, ToolRegistry())
    result = mgr.start_oauth("nonexistent")
    assert result["ok"] is False
    assert "unknown" in result["error"]


def test_start_oauth_stdio_server_returns_error() -> None:
    cfg = _cfg("stdio-srv", enabled=False)
    mgr = McpManager({"stdio-srv": cfg}, ToolRegistry())
    result = mgr.start_oauth("stdio-srv")
    assert result["ok"] is False
    assert "http" in result["error"]


def test_start_oauth_non_oauth_http_returns_error() -> None:
    cfg = _http_cfg("token-srv", auth_type="token")
    mgr = McpManager({"token-srv": cfg}, ToolRegistry())
    result = mgr.start_oauth("token-srv")
    assert result["ok"] is False
    assert "oauth" in result["error"]


def test_start_oauth_none_auth_http_returns_error() -> None:
    cfg = _http_cfg("none-srv", auth_type="none")
    mgr = McpManager({"none-srv": cfg}, ToolRegistry())
    result = mgr.start_oauth("none-srv")
    assert result["ok"] is False
    assert "oauth" in result["error"]


def test_start_oauth_valid_calls_connect_and_returns_ok() -> None:
    from unittest.mock import MagicMock

    cfg = _http_cfg("oauth-srv", auth_type="oauth")
    mgr = McpManager({"oauth-srv": cfg}, ToolRegistry())

    # Replace connect with a spy so no real event loop or worker is spawned.
    mock_connect = MagicMock()
    mgr.connect = mock_connect  # type: ignore[method-assign]

    result = mgr.start_oauth("oauth-srv")

    assert result == {"ok": True, "started": True}
    mock_connect.assert_called_once_with("oauth-srv")


def test_start_oauth_valid_disconnects_if_already_connected() -> None:
    from unittest.mock import MagicMock, patch

    cfg = _http_cfg("oauth-srv2", auth_type="oauth")
    mgr = McpManager({"oauth-srv2": cfg}, ToolRegistry())

    # Seed _workers to simulate an already-connected server.
    mgr._workers["oauth-srv2"] = object()  # type: ignore[assignment]

    mock_disconnect = MagicMock()
    mock_connect = MagicMock()
    with (
        patch.object(mgr, "disconnect", mock_disconnect),
        patch.object(mgr, "connect", mock_connect),
    ):
        result = mgr.start_oauth("oauth-srv2")

    assert result == {"ok": True, "started": True}
    mock_disconnect.assert_called_once_with("oauth-srv2")
    mock_connect.assert_called_once_with("oauth-srv2")


def test_concurrent_status_and_crud_does_not_raise(tmp_path: Path) -> None:
    """RLock stress test: concurrent status() reads and add/remove CRUD must not raise."""
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)

    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                mgr.status()
            except Exception as exc:
                errors.append(exc)
                stop.set()

    def writer() -> None:
        counter = 0
        while not stop.is_set():
            sid = f"srv{counter % 3}"
            try:
                mgr.add_or_update_server(
                    {
                        "id": sid,
                        "label": sid,
                        "transport": "stdio",
                        "command": "python",
                        "args": [],
                        "enabled": False,
                    }
                )
                mgr.remove_server(sid)
            except Exception as exc:
                errors.append(exc)
                stop.set()
            counter += 1

    t_reader = threading.Thread(target=reader, daemon=True)
    t_writer = threading.Thread(target=writer, daemon=True)
    t_reader.start()
    t_writer.start()

    time.sleep(0.3)
    stop.set()
    t_reader.join(timeout=2.0)
    t_writer.join(timeout=2.0)

    assert not errors, f"concurrent access raised: {errors}"
