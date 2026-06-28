"""Owns all configured MCP servers and the single event loop they run on.

The MCP SDK is asyncio-based, so the manager runs one event loop on a dedicated
daemon thread and drives every server's :class:`~autobot.mcp.session.McpServerWorker`
on it. Its public API is synchronous (the daemon and the composition root call it
from other threads); it schedules work onto the loop via ``run_coroutine_threadsafe``
and ``call_soon_threadsafe``. No ``mcp`` SDK import lives here — the worker owns that,
lazily — so importing the manager needs no extra.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger
from autobot.mcp.config import DEFAULT_MCP_CONFIG_PATH
from autobot.mcp.session import McpServerWorker

if TYPE_CHECKING:
    from collections.abc import Callable

    from autobot.mcp.config import McpServerConfig
    from autobot.tools.registry import ToolRegistry

_log = get_logger("mcp")


class McpManager:
    """Lifecycle manager for MCP servers on a shared background event loop."""

    def __init__(
        self,
        config: dict[str, McpServerConfig],
        registry: ToolRegistry,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        config_path: str | Path = DEFAULT_MCP_CONFIG_PATH,
    ) -> None:
        self._config = config
        self._registry = registry
        self._on_event = on_event
        self._config_path = Path(config_path).expanduser()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._workers: dict[str, McpServerWorker] = {}
        self._futures: dict[str, concurrent.futures.Future[None]] = {}

    def start(self) -> None:
        """Start the background event loop thread (idempotent)."""
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-loop", daemon=True)
        self._thread.start()
        _log.info("mcp loop started")

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect_enabled(self) -> None:
        """Connect every server whose config has ``enabled=True``."""
        for server_id, cfg in self._config.items():
            if cfg.enabled:
                self.connect(server_id)

    def connect(self, server_id: str) -> None:
        """Spawn a worker for ``server_id`` (no-op if unknown or already connected)."""
        if self._loop is None:
            self.start()
        assert self._loop is not None
        cfg = self._config.get(server_id)
        if cfg is None or server_id in self._workers:
            return
        worker = McpServerWorker(cfg, self._registry, loop=self._loop, on_event=self._on_event)
        self._workers[server_id] = worker
        self._futures[server_id] = asyncio.run_coroutine_threadsafe(worker.run(), self._loop)
        _log.info("mcp connecting server=%s", server_id)

    def disconnect(self, server_id: str, timeout: float = 5.0) -> None:
        """Ask a server's worker to exit and wait briefly for it to unwind."""
        worker = self._workers.pop(server_id, None)
        future = self._futures.pop(server_id, None)
        if worker is not None:
            worker.request_shutdown()
        if future is not None:
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                _log.warning(
                    "mcp worker did not stop within %.0fs server=%s; cancelling",
                    timeout,
                    server_id,
                )
                # best-effort: cancel() interrupts a waiting Future but cannot stop a
                # running coroutine; the real shutdown path is the loop.stop() in shutdown().
                future.cancel()
            except Exception:  # an unexpected worker error must not break shutdown
                _log.exception("mcp worker raised on disconnect server=%s", server_id)
                # best-effort: cancel() interrupts a waiting Future but cannot stop a
                # running coroutine; the real shutdown path is the loop.stop() in shutdown().
                future.cancel()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Disconnect all servers, stop the loop thread, and close the loop (idempotent).

        Safe before ``start()`` and across restart cycles: the loop is closed so its
        file descriptors / executor are released, and a later ``start()`` builds a
        fresh one (so a reloadable manager can stop and restart cleanly).
        """
        for server_id in list(self._workers):
            self.disconnect(server_id, timeout=timeout)
        loop = self._loop
        thread = self._thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)
        if loop is not None and not loop.is_closed():
            loop.close()
        self._thread = None
        self._loop = None
        _log.info("mcp loop stopped")

    def status(self) -> list[dict[str, Any]]:
        """A status row per configured server (for the daemon / Settings view)."""
        rows: list[dict[str, Any]] = []
        for server_id, cfg in self._config.items():
            worker = self._workers.get(server_id)
            rows.append(
                {
                    "server": server_id,
                    "label": cfg.label,
                    "enabled": cfg.enabled,
                    "egress": cfg.egress,
                    "auth_type": cfg.auth_type,
                    "state": worker.state if worker is not None else "disconnected",
                    "tool_count": worker.tool_count if worker is not None else 0,
                }
            )
        return rows

    def add_or_update_server(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, and (re)connect one server descriptor.

        Uses :func:`~autobot.mcp.config._coerce_server` for validation so the same
        rules that govern ``servers.json`` loading apply here. On success the live
        config is updated, the file is saved, and — if the manager is running and the
        server was previously connected — the worker is restarted so it picks up the
        new frozen config.

        Args:
            descriptor: Raw JSON object (must include ``id`` and a valid ``transport``).

        Returns:
            ``{"ok": True, "server": status_row}`` or ``{"ok": False, "error": str}``.
        """
        from autobot.mcp.config import _coerce_server, save_mcp_config

        server_id = str(descriptor.get("id", ""))
        if not server_id:
            return {"ok": False, "error": "descriptor must include 'id'"}
        cfg = _coerce_server(server_id, descriptor)
        if cfg is None:
            error_msg = f"invalid transport or descriptor for server {server_id!r}"
            return {"ok": False, "error": error_msg}
        was_connected = server_id in self._workers
        if was_connected:
            self.disconnect(server_id)
        self._config[server_id] = cfg
        save_mcp_config(self._config, self._config_path)
        if was_connected and cfg.enabled and self._loop is not None:
            self.connect(server_id)
        rows = [r for r in self.status() if r["server"] == server_id]
        return {"ok": True, "server": rows[0] if rows else {"server": server_id}}

    def remove_server(self, server_id: str) -> bool:
        """Disconnect (if active), remove from config, and persist.

        Args:
            server_id: The server to remove.

        Returns:
            ``True`` if found and removed, ``False`` if unknown.
        """
        from autobot.mcp.config import save_mcp_config

        if server_id not in self._config:
            return False
        if server_id in self._workers:
            self.disconnect(server_id)
        del self._config[server_id]
        save_mcp_config(self._config, self._config_path)
        return True

    def set_enabled(self, server_id: str, enabled: bool) -> bool:
        """Toggle ``enabled``, persist, and reconnect if the manager is running.

        Args:
            server_id: The server to toggle.
            enabled: ``True`` to enable (connect), ``False`` to disable (disconnect).

        Returns:
            ``True`` if the server was found, ``False`` if unknown.
        """
        import dataclasses

        from autobot.mcp.config import save_mcp_config

        cfg = self._config.get(server_id)
        if cfg is None:
            return False
        if server_id in self._workers:
            self.disconnect(server_id)
        self._config[server_id] = dataclasses.replace(cfg, enabled=enabled)
        save_mcp_config(self._config, self._config_path)
        if enabled and self._loop is not None:
            self.connect(server_id)
        return True

    def tools_for(self, server_id: str) -> list[dict[str, Any]]:
        """Return the cached all-tools list for ``server_id`` (empty if not connected).

        Uses :meth:`~autobot.mcp.session.McpServerWorker.all_tools`, which returns a
        copy of the full pre-filter snapshot so the UI can show disabled tools.

        Args:
            server_id: The server whose tools to return.

        Returns:
            A list of dicts, each ``{name, description, risk, network, enabled}``,
            or ``[]`` if the server is not connected or not configured.
        """
        worker = self._workers.get(server_id)
        if worker is None:
            return []
        return worker.all_tools()

    def set_tool_override(
        self,
        server_id: str,
        tool: str,
        *,
        risk: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Adjust a tool's risk or enable/disable it, persist + reconnect.

        Disable: adds ``tool`` to ``cfg.tool_deny`` (as a new frozen tuple).
        Enable: removes ``tool`` from ``cfg.tool_deny``.
        Risk: sets ``cfg.tool_risk_overrides[tool] = risk`` (or removes it if
        ``risk`` is ``None`` and the key is present).

        Args:
            server_id: The server that owns the tool.
            tool: The bare tool name (without namespace prefix).
            risk: Optional risk string (``"read_only"``, ``"write"``, ``"destructive"``).
            enabled: Optional enable/disable override.

        Returns:
            ``True`` if the server was found, ``False`` if unknown.
        """
        import dataclasses

        from autobot.mcp.config import save_mcp_config

        cfg = self._config.get(server_id)
        if cfg is None:
            return False
        deny = list(cfg.tool_deny)
        overrides = dict(cfg.tool_risk_overrides)
        if enabled is False and tool not in deny:
            deny.append(tool)
        elif enabled is True and tool in deny:
            deny.remove(tool)
        if risk is not None:
            overrides[tool] = risk
        was_connected = server_id in self._workers
        if was_connected:
            self.disconnect(server_id)
        self._config[server_id] = dataclasses.replace(
            cfg, tool_deny=tuple(deny), tool_risk_overrides=overrides
        )
        save_mcp_config(self._config, self._config_path)
        if was_connected and self._config[server_id].enabled and self._loop is not None:
            self.connect(server_id)
        return True

    def secret_present(self, server_id: str) -> bool:
        """Whether the Keychain secret referenced by this server's config is set.

        Args:
            server_id: The server to check.

        Returns:
            ``True`` if ``cfg.secret_ref`` is non-None and the secret exists in the
            Keychain; ``False`` otherwise (unknown server, no secret_ref, or unset key).
        """
        from autobot.secrets import has_secret

        cfg = self._config.get(server_id)
        if cfg is None or cfg.secret_ref is None:
            return False
        return has_secret(cfg.secret_ref)
