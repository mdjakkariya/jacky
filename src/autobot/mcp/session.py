"""One MCP server's connection lifecycle, driven on the manager's event loop.

The MCP SDK is asyncio/anyio with structured concurrency: a session's transport
context managers and all of its calls must run on ONE task on ONE loop. So each
server gets a long-lived worker coroutine (:meth:`McpServerWorker.run`) that
enters the transport + ``ClientSession`` context managers, initializes, lists and
registers tools, then serves commands from an :class:`asyncio.Queue` until
shutdown. Synchronous tool handlers submit a :class:`_Call` via the loop and block
on a :class:`concurrent.futures.Future`; MCP errors/timeouts are raised so
``ToolRegistry.dispatch`` turns them into failed ``ToolResult``s. The heavy ``mcp``
SDK is **lazy-imported inside methods**, so importing this module needs no extra.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger
from autobot.mcp import adapter
from autobot.mcp.auth import stdio_env_for
from autobot.secrets import get_secret as _get_secret
from autobot.tools.registry import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from autobot.mcp.config import McpServerConfig
    from autobot.tools.registry import ToolRegistry

_log = get_logger("mcp")

# How long a synchronous handler waits for a tool result before giving up. A slow
# remote tool returns a failed ToolResult (timeout) rather than blocking a turn forever.
CALL_TIMEOUT_S = 30.0


def tool_allowed(name: str, allow: tuple[str, ...], deny: tuple[str, ...]) -> bool:
    """Whether a tool name passes the server's allow/deny globs.

    A deny match always excludes. With a non-empty ``allow`` list, only names
    matching at least one allow glob are kept; an empty ``allow`` permits all.
    """
    if deny and any(fnmatch(name, pat) for pat in deny):
        return False
    if allow:
        return any(fnmatch(name, pat) for pat in allow)
    return True


@dataclass
class _Call:
    """A queued tool invocation awaiting a result on its future."""

    tool: str
    args: dict[str, Any]
    future: concurrent.futures.Future[str]


class McpServerWorker:
    """Owns one server's connection, tool registration, and call serialization."""

    def __init__(
        self,
        config: McpServerConfig,
        registry: ToolRegistry,
        *,
        loop: asyncio.AbstractEventLoop,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = config
        self._registry = registry
        self._loop = loop
        self._on_event = on_event
        self._queue: asyncio.Queue[_Call | str] | None = None
        self._registered: list[str] = []
        self._state = "disconnected"
        self._tool_count = 0
        # Full snapshot of the server's tool list (built before allow/deny filtering).
        # Lets the UI show disabled tools and their risk; the registry only gets the
        # filtered subset. GIL-safe reads via the all_tools() copy method.
        self._all_tools: list[dict[str, object]] = []

    @property
    def state(self) -> str:
        """One of ``"disconnected"``, ``"connected"``, ``"error"``."""
        return self._state

    @property
    def tool_count(self) -> int:
        """Number of tools currently registered from this server."""
        return self._tool_count

    def all_tools(self) -> list[dict[str, object]]:
        """Return a copy of the full pre-filter tool snapshot.

        Includes tools that are excluded by ``tool_deny`` / ``tool_allow`` (with
        ``enabled=False``), so the UI can show and toggle them. The list is rebuilt
        on every ``_sync_tools`` call (connect and ``tools/list_changed`` resync).

        Returns:
            A copy of ``_all_tools``; each item is ``{name, description, risk, network, enabled}``.
        """
        return list(self._all_tools)

    # --- synchronous entry points (called from the engine thread) ---

    def submit_call(self, tool: str, args: dict[str, Any]) -> str:
        """Run a tool call on the worker's loop and block for the result.

        Raises ``RuntimeError`` if the server is not connected or the call times
        out, and re-raises a tool's own error — ``ToolRegistry.dispatch`` converts
        any of these into a failed ``ToolResult`` (so nothing escapes dispatch).
        """
        if self._queue is None or self._state != "connected":
            raise RuntimeError(f"MCP server {self._cfg.id!r} is not connected")
        future: concurrent.futures.Future[str] = concurrent.futures.Future()
        call = _Call(tool=tool, args=args, future=future)
        self._loop.call_soon_threadsafe(self._enqueue, call)
        try:
            return future.result(timeout=CALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            raise RuntimeError(
                f"MCP tool {tool!r} on {self._cfg.id!r} timed out after {CALL_TIMEOUT_S}s"
            ) from exc

    def request_shutdown(self) -> None:
        """Ask the worker coroutine to exit (thread-safe)."""
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._enqueue, "shutdown")

    def _enqueue(self, item: _Call | str) -> None:
        """Put an item on the queue from the loop thread (created lazily in run())."""
        if self._queue is not None:
            self._queue.put_nowait(item)

    # --- the worker coroutine (runs on the manager's loop) ---

    async def run(self) -> None:
        """Connect, register tools, serve calls until shutdown, then clean up.

        Never raises: any failure marks the server ``"error"`` and unregisters its
        tools, so a bad server can't crash the loop or a turn.
        """
        self._queue = asyncio.Queue()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self._cfg.command or "",
                args=list(self._cfg.args),
                env=stdio_env_for(self._cfg, _get_secret),
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write, message_handler=self._on_message) as session,
            ):
                await session.initialize()
                await self._sync_tools(session)
                self._state = "connected"
                self._emit_status()
                _log.info("mcp connected server=%s tools=%d", self._cfg.id, self._tool_count)
                await self._serve(session)
        except Exception as exc:  # never let the worker crash the loop
            self._state = "error"
            _log.exception("mcp worker failed server=%s", self._cfg.id)
            self._emit_status(error=str(exc))
        finally:
            # Flip off "connected" first so new submit_call()s are rejected fast,
            # then fail any calls still queued/in-flight so their callers don't have
            # to wait out the full CALL_TIMEOUT_S on a shutdown or crash.
            if self._state == "connected":
                self._state = "disconnected"
            self._fail_pending()
            self._unregister_all()
            self._tool_count = 0
            self._emit_status()
            _log.info("mcp disconnected server=%s", self._cfg.id)

    async def _serve(self, session: Any) -> None:
        """Process queued commands until a shutdown sentinel arrives."""
        assert self._queue is not None
        while True:
            cmd = await self._queue.get()
            if cmd == "shutdown":
                return
            if cmd == "resync":
                await self._sync_tools(session)
                continue
            if isinstance(cmd, _Call):
                await self._do_call(session, cmd)

    def _fail_pending(self) -> None:
        """Resolve every still-queued call with an error (called on worker exit).

        Without this, a call enqueued behind the shutdown sentinel (or in flight when
        the worker crashes) would never have its future set, forcing the blocked
        ``submit_call`` caller to wait the full ``CALL_TIMEOUT_S`` instead of failing
        fast. Drains the queue and fails each pending :class:`_Call`.
        """
        if self._queue is None:
            return
        exc = RuntimeError(f"MCP server {self._cfg.id!r} disconnected")
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _Call) and not item.future.done():
                item.future.set_exception(exc)

    async def _do_call(self, session: Any, call: _Call) -> None:
        """Invoke one tool and resolve its future (text on ok, exception on error)."""
        try:
            result = await session.call_tool(call.tool, call.args)
            text, is_error = adapter.result_to_text(result)
            if is_error:
                call.future.set_exception(RuntimeError(text))
            else:
                call.future.set_result(text)
        except Exception as exc:  # surface to the waiting handler, don't crash the loop
            if not call.future.done():
                call.future.set_exception(exc)

    async def _sync_tools(self, session: Any) -> None:
        """List the server's tools and reconcile the registry (add/replace/remove)."""
        listed = await session.list_tools()
        floor = adapter.risk_from_name(self._cfg.default_risk)
        overrides = {
            name: adapter.risk_from_name(value)
            for name, value in self._cfg.tool_risk_overrides.items()
        }
        network = self._cfg.egress == "network"
        # Build the full snapshot (all tools, pre-filter) for the UI.
        self._all_tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "risk": adapter.risk_for(t, floor=floor, overrides=overrides).name.lower(),
                "network": network,
                "enabled": tool_allowed(t.name, self._cfg.tool_allow, self._cfg.tool_deny),
            }
            for t in listed.tools
        ]
        desired: dict[str, ToolSpec] = {}
        for tool in listed.tools:
            if not tool_allowed(tool.name, self._cfg.tool_allow, self._cfg.tool_deny):
                continue
            reg_name = adapter.namespaced(self._cfg.id, tool.name)
            desired[reg_name] = ToolSpec(
                name=reg_name,
                description=tool.description or "",
                parameters=adapter.params_from_input_schema(tool.inputSchema),
                handler=self._make_handler(tool.name),
                risk=adapter.risk_for(tool, floor=floor, overrides=overrides),
                network=network,
            )
        for name in list(self._registered):
            if name not in desired:
                self._registry.unregister(name)
        for _name, spec in desired.items():
            self._registry.register(spec, replace=True)
        self._registered = list(desired)
        self._tool_count = len(desired)
        _log.info("mcp tools synced server=%s count=%d", self._cfg.id, self._tool_count)

    def _make_handler(self, bare_tool: str) -> Callable[..., str]:
        """Build a synchronous ToolSpec handler that routes through ``submit_call``."""

        def handler(**kwargs: Any) -> str:
            return self.submit_call(bare_tool, kwargs)

        return handler

    async def _on_message(self, message: Any) -> None:
        """SDK message hook: enqueue a resync when the server's tool list changes."""
        from mcp.types import ServerNotification, ToolListChangedNotification

        if isinstance(message, ServerNotification) and isinstance(
            message.root, ToolListChangedNotification
        ):
            self._enqueue("resync")

    def _unregister_all(self) -> None:
        """Remove every tool this worker registered."""
        for name in self._registered:
            self._registry.unregister(name)
        self._registered = []

    def _emit_status(self, error: str | None = None) -> None:
        """Publish a status event if a sink is wired (never raises)."""
        if self._on_event is None:
            return
        payload: dict[str, Any] = {
            "type": "mcp_status",
            "server": self._cfg.id,
            "state": self._state,
            "tool_count": self._tool_count,
        }
        if error:
            payload["error"] = error
        try:
            self._on_event(payload)
        except Exception:  # a UI hiccup must never break the worker
            _log.debug("mcp on_event sink failed", exc_info=True)
