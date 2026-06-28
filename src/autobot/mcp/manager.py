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
from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger
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
    ) -> None:
        self._config = config
        self._registry = registry
        self._on_event = on_event
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
                future.cancel()
            except Exception:  # an unexpected worker error must not break shutdown
                _log.exception("mcp worker raised on disconnect server=%s", server_id)
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
                    "state": worker.state if worker is not None else "disconnected",
                    "tool_count": worker.tool_count if worker is not None else 0,
                }
            )
        return rows
