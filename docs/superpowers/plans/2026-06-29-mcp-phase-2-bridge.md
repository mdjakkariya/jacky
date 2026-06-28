# MCP Phase 2 — Async Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Connect to local **stdio** MCP servers, list their tools, register them as gated `ToolSpec`s, call them through the existing permission gate, re-sync on `tools/list_changed`, and shut down cleanly — driven by a dedicated event-loop thread with one long-lived worker coroutine per server. Plus the gate's network-egress confirm rule.

**Architecture:** The MCP SDK is asyncio/anyio with structured concurrency, so a session's context managers AND all its calls must live on one task on one loop. `McpManager` owns one event loop on a daemon thread; each server runs a long-lived `McpServerWorker.run()` coroutine that enters the transport + `ClientSession` context managers, initializes, lists+registers tools, then serves `_Call`/`"resync"`/`"shutdown"` commands from an `asyncio.Queue` until shutdown. Synchronous tool handlers submit a `_Call` to the loop and block on a `concurrent.futures.Future`; MCP errors/timeouts are raised so `ToolRegistry.dispatch` converts them to failed `ToolResult`s. The `mcp` SDK is **lazy-imported inside methods**, so the modules import (and most tests run) without the extra.

**Tech Stack:** Python 3.11, asyncio + threading, the `mcp` SDK (`>=1.28,<2`, opt-in extra — verified against installed **1.28.0**), pytest, mypy strict.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- mypy **strict** over `src` AND `tests`. The `mcp` SDK is an opt-in extra **not in the base env**, so its imports must be in mypy's `ignore_missing_imports` list (Task 1) and **lazy-imported inside functions/methods** (never at module top) so `import autobot.mcp.manager` / `autobot.mcp.session` works with the SDK absent.
- Google-style docstrings on every public module/class/function (ruff `D`); tests exempt.
- Line length 100; format with `uv run ruff format .`.
- Value objects `frozen=True, slots=True`.
- Tools never raise out of `ToolRegistry.dispatch` — the dispatch already wraps handlers; an MCP handler **may raise** on error (dispatch converts it to a failed `ToolResult`). Worker code must never let an exception escape the worker coroutine and crash the loop.
- `[mcp]` logger (`get_logger("mcp")`) at the seams (connect, disconnect, tools synced, call errors) — no per-call/per-token spam.
- Privacy: this phase adds **local stdio** servers only (`egress: "local"`, `network=False`); no off-device path yet. The gate's network rule is added now but dormant until a network server exists (Phase 3).
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- Gate: `make check` green before each task is done. Run single tests with `uv run pytest tests/unit/<file>.py -v`. The **integration test (Task 6) needs the extra**: `uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v`.

**Interfaces produced by Phase 1 (consume these — already on the branch):**
- `autobot.mcp.adapter`: `namespaced(server_id, tool)`, `split_namespaced(name)`, `params_from_input_schema(schema)`, `result_to_text(result) -> (str, bool)`, `risk_for(tool, *, floor, overrides) -> Risk`, `risk_from_name(name, default=Risk.WRITE) -> Risk`, `fingerprint(tool) -> str`.
- `autobot.mcp.config`: `McpServerConfig` (fields: id, label, transport, command, args:tuple, env:dict, url, auth_type, token_env, secret_ref, enabled, egress, default_risk, tool_allow:tuple, tool_deny:tuple, tool_risk_overrides:dict), `load_mcp_config(path)`, `DEFAULT_MCP_CONFIG_PATH`.
- `autobot.tools.registry`: `ToolSpec(..., network: bool = False)`, `ToolRegistry.register(spec, *, replace=False)`, `ToolRegistry.unregister(name) -> bool`.
- `autobot.core.types.Risk` is an `IntEnum`: `READ_ONLY=0 < WRITE=1 < DESTRUCTIVE=2`.

**Branch:** continue on `feat/mcp-phase-1-core` (it carries Phase 1 + design + plan). All Phase-2 commits stack here.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` (modify) | Add `mcp.*` to mypy `ignore_missing_imports` |
| `src/autobot/tools/permission.py` (modify) | Network-egress confirm rule + per-call confirm `kind` |
| `src/autobot/mcp/session.py` (create) | `McpServerWorker` (per-server worker coroutine) + pure `tool_allowed()` |
| `src/autobot/mcp/manager.py` (create) | `McpManager` (loop thread, connect/disconnect/shutdown/status) |
| `src/autobot/app.py` (modify) | `if settings.allow_mcp:` block — build/start/connect manager, atexit shutdown |
| `tests/unit/test_permission_gate.py` (modify) | Network confirm-rule + kind tests |
| `tests/unit/test_mcp_session.py` (create) | `tool_allowed` glob unit tests |
| `tests/unit/test_mcp_manager.py` (create) | Manager lifecycle + graceful-degradation tests (no live server) |
| `tests/integration/__init__.py` (create) | Integration test package marker |
| `tests/integration/echo_mcp_server.py` (create) | Tiny FastMCP stdio echo server (test fixture) |
| `tests/integration/test_mcp_integration.py` (create) | End-to-end: connect → list → call → shutdown |

---

### Task 1: mypy override for the lazy `mcp` import

**Files:** Modify `pyproject.toml` (the `[[tool.mypy.overrides]]` `module` list).

**Interfaces:** Produces nothing in code; lets later tasks `from mcp import ...` (lazily) without mypy "missing import" errors, since the extra is not in the base env.

- [ ] **Step 1: Add `mcp.*` to the override list**

In `pyproject.toml`, in the existing `[[tool.mypy.overrides]]` block whose `module = [...]` lists third-party runtimes (faster_whisper.*, …, anthropic.*, …), add `"mcp.*",` to that list (alphabetical-ish placement is fine; e.g. after `"anthropic.*",`).

- [ ] **Step 2: Verify mypy still green**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(mcp): let mypy treat the optional mcp SDK as a missing-stub import"
```

---

### Task 2: Gate network-egress confirm rule + confirm `kind`

**Files:**
- Modify: `src/autobot/tools/permission.py` (`PermissionGate.execute` + a new `_confirm_kind` helper)
- Test: `tests/unit/test_permission_gate.py`

**Interfaces:**
- Consumes: `ToolSpec.network`, `Risk` ordering.
- Produces: gate now confirms when `risk >= threshold OR (spec.network and risk >= Risk.WRITE)`, and passes a `kind` to `Confirmer.confirm`: `"network"` for egress, else `"danger"` for destructive, else `"write"`.

**Context:** `PermissionGate.__init__` stores `self._threshold` (default `Risk.DESTRUCTIVE`). Today `execute()` does `if spec.risk >= self._threshold:` then `if not self._confirmer.confirm(prompt):` (no `kind` passed → confirmer default `"danger"`). The `Confirmer.confirm(self, prompt, kind="danger")` protocol already accepts `kind`. This task makes egress writes confirm (even below the destructive threshold) and tints the card.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_permission_gate.py` (it already constructs gates with confirmers; reuse its existing imports — ensure `ToolSpec`, `ToolRegistry`, `Risk`, `ToolCall`, `AuditLog`, and the `AlwaysAllow`/`AlwaysDeny` confirmers from `autobot.tools.permission` are imported; add any missing). Add a small recording confirmer to capture `kind`:

```python
class _RecordingConfirmer:
    """Confirmer that approves and records the kind it was asked with."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        self.kinds.append(kind)
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default


def _gate(confirmer: object) -> tuple[ToolRegistry, PermissionGate]:
    reg = ToolRegistry()
    gate = PermissionGate(reg, AuditLog(":memory:"), confirmer)  # type: ignore[arg-type]
    return reg, gate


def test_network_write_tool_is_confirmed_with_network_kind() -> None:
    rec = _RecordingConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(name="slack__send", description="", parameters={}, handler=lambda: "sent",
                 risk=Risk.WRITE, network=True)
    )
    result = gate.execute(ToolCall(name="slack__send", arguments={}))
    assert result.ok is True
    assert rec.kinds == ["network"]


def test_network_readonly_tool_is_not_confirmed() -> None:
    rec = _RecordingConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(name="slack__search", description="", parameters={}, handler=lambda: "hits",
                 risk=Risk.READ_ONLY, network=True)
    )
    result = gate.execute(ToolCall(name="slack__search", arguments={}))
    assert result.ok is True
    assert rec.kinds == []  # network READ_ONLY: badge only, no card


def test_local_write_tool_is_not_confirmed() -> None:
    rec = _RecordingConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(name="local_write", description="", parameters={}, handler=lambda: "ok",
                 risk=Risk.WRITE, network=False)
    )
    gate.execute(ToolCall(name="local_write", arguments={}))
    assert rec.kinds == []  # local WRITE stays silent (unchanged behavior)


def test_destructive_tool_confirmed_with_danger_kind() -> None:
    rec = _RecordingConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(name="wipe", description="", parameters={}, handler=lambda: "gone",
                 risk=Risk.DESTRUCTIVE, network=False)
    )
    gate.execute(ToolCall(name="wipe", arguments={}))
    assert rec.kinds == ["danger"]


def test_network_destructive_kind_is_network() -> None:
    rec = _RecordingConfirmer()
    reg, gate = _gate(rec)
    reg.register(
        ToolSpec(name="slack__delete", description="", parameters={}, handler=lambda: "x",
                 risk=Risk.DESTRUCTIVE, network=True)
    )
    gate.execute(ToolCall(name="slack__delete", arguments={}))
    assert rec.kinds == ["network"]  # egress tint takes precedence
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_permission_gate.py -k "network or local_write or danger_kind or destructive_tool_confirmed" -v`
Expected: FAIL — network WRITE currently is NOT confirmed (kinds empty), and no `kind` is passed.

- [ ] **Step 3: Implement the rule**

In `src/autobot/tools/permission.py`, in `PermissionGate.execute`, replace the confirmation block. Find:

```python
        if spec.risk >= self._threshold:
            prompt = spec.confirm_prompt or self._format_prompt(
                spec.name, spec.risk, call.arguments
            )
            if not self._confirmer.confirm(prompt):
```

Replace with:

```python
        if spec.risk >= self._threshold or (spec.network and spec.risk >= Risk.WRITE):
            prompt = spec.confirm_prompt or self._format_prompt(
                spec.name, spec.risk, call.arguments
            )
            if not self._confirmer.confirm(prompt, self._confirm_kind(spec)):
```

Then add this static helper to the `PermissionGate` class (e.g. just above `_format_prompt`):

```python
    @staticmethod
    def _confirm_kind(spec: ToolSpec) -> str:
        """Card tone for a confirmation: egress > destructive > write.

        ``"network"`` tints the card for an off-device send (the orange "data path"
        card); otherwise ``"danger"`` for a destructive action and ``"write"`` for a
        reversible change. Lets the UI make the off-device moment unmistakable.
        """
        if spec.network:
            return "network"
        if spec.risk >= Risk.DESTRUCTIVE:
            return "danger"
        return "write"
```

Ensure `ToolSpec` is imported in `permission.py` (it imports from `autobot.tools.registry` already for `ToolRegistry`; add `ToolSpec` to that import if not present). `Risk` is already imported.

- [ ] **Step 4: Run tests (new + existing) to verify pass**

Run: `uv run pytest tests/unit/test_permission_gate.py -v`
Expected: PASS — new tests green AND all pre-existing gate tests still pass (destructive still confirms; the only added confirmations are network writes).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/permission.py tests/unit/test_permission_gate.py
git commit -m "feat(gate): confirm network-egress writes + per-call confirm kind"
```

---

### Task 3: `McpServerWorker` (per-server worker coroutine)

**Files:**
- Create: `src/autobot/mcp/session.py`
- Test: `tests/unit/test_mcp_session.py`

**Interfaces:**
- Consumes: `adapter`, `McpServerConfig`, `ToolRegistry`/`ToolSpec`, `Risk`.
- Produces:
  - `tool_allowed(name: str, allow: tuple[str, ...], deny: tuple[str, ...]) -> bool` (pure).
  - `McpServerWorker(config, registry, *, loop, on_event=None)` with sync `submit_call(tool, args) -> str`, `request_shutdown()`, properties `state: str` / `tool_count: int`, and the async `run()` coroutine. Registers `<id>__<tool>` specs; handlers route through `submit_call`; MCP errors/timeouts raise (→ failed `ToolResult`).

- [ ] **Step 1: Write the failing tests** (pure helper only — the connect path is covered by the Task 6 integration test)

Create `tests/unit/test_mcp_session.py`:

```python
"""Unit tests for the pure parts of the MCP session worker (no SDK, no subprocess)."""

from __future__ import annotations

from autobot.mcp.session import tool_allowed


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_mcp_session.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.mcp.session`.

- [ ] **Step 3: Write the module**

Create `src/autobot/mcp/session.py`:

```python
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

    @property
    def state(self) -> str:
        """One of ``"disconnected"``, ``"connected"``, ``"error"``."""
        return self._state

    @property
    def tool_count(self) -> int:
        """Number of tools currently registered from this server."""
        return self._tool_count

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
                env=dict(self._cfg.env) or None,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, message_handler=self._on_message) as session:
                    await session.initialize()
                    await self._sync_tools(session)
                    self._state = "connected"
                    self._emit_status()
                    _log.info(
                        "mcp connected server=%s tools=%d", self._cfg.id, self._tool_count
                    )
                    await self._serve(session)
        except Exception as exc:  # never let the worker crash the loop
            self._state = "error"
            _log.exception("mcp worker failed server=%s", self._cfg.id)
            self._emit_status(error=str(exc))
        finally:
            self._unregister_all()
            if self._state != "error":
                self._state = "disconnected"
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
        for name, spec in desired.items():
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
```

- [ ] **Step 4: Run the unit tests + mypy**

Run: `uv run pytest tests/unit/test_mcp_session.py -v` → PASS (4).
Run: `uv run mypy` → `Success`.
Run: `uv run python -c "import autobot.mcp.session; print('import-ok')"` → prints `import-ok` (proves no SDK needed to import).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/mcp/session.py tests/unit/test_mcp_session.py
git commit -m "feat(mcp): per-server worker coroutine (connect/list/register/call/resync)"
```

---

### Task 4: `McpManager` (loop thread + lifecycle)

**Files:**
- Create: `src/autobot/mcp/manager.py`
- Test: `tests/unit/test_mcp_manager.py`

**Interfaces:**
- Consumes: `McpServerWorker`, `McpServerConfig`, `ToolRegistry`.
- Produces: `McpManager(config: dict[str, McpServerConfig], registry, *, on_event=None)` with `start()`, `connect_enabled()`, `connect(server_id)`, `disconnect(server_id, timeout=5.0)`, `shutdown(timeout=5.0)`, `status() -> list[dict]`. Owns one event loop on a daemon thread.

- [ ] **Step 1: Write the failing tests** (lifecycle + graceful degradation; no live server)

Create `tests/unit/test_mcp_manager.py`:

```python
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
        id=server_id, label=server_id, transport="stdio",
        command="this-command-does-not-exist-xyz", args=(), enabled=enabled,
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
    mgr = McpManager(
        {"a": _cfg("a", enabled=False), "b": _cfg("b", enabled=False)}, ToolRegistry()
    )
    ids = {s["server"] for s in mgr.status()}
    assert ids == {"a", "b"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_mcp_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.mcp.manager`.

- [ ] **Step 3: Write the module**

Create `src/autobot/mcp/manager.py`:

```python
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
            except (concurrent.futures.TimeoutError, Exception):  # noqa: BLE001
                future.cancel()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Disconnect all servers and stop the loop thread (idempotent)."""
        for server_id in list(self._workers):
            self.disconnect(server_id, timeout=timeout)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
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
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_mcp_manager.py -v` → PASS (4). (The bad-server test exercises graceful degradation: in the base env the worker's lazy `from mcp import ...` raises ImportError → `state="error"`; with the extra installed, the bogus command fails to spawn → also `"error"`. Either way no crash, no tools registered.)
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/mcp/manager.py tests/unit/test_mcp_manager.py
git commit -m "feat(mcp): McpManager — background loop, connect/disconnect/shutdown/status"
```

---

### Task 5: Wire the manager into `app.py::build()`

**Files:** Modify `src/autobot/app.py`.

**Interfaces:** Consumes `Settings.allow_mcp`, `McpManager`, `load_mcp_config`. Produces the running subsystem: when `allow_mcp`, the manager starts, connects enabled servers (registering their tools into the same `registry` the LLM/gate use), and is shut down at process exit.

**Context:** `build()` constructs `registry` early and registers built-ins / opt-in tool families (see the `if settings.allow_web:` block ~line 441). Add the MCP block in the same spirit. The manager only needs `registry`; it does not change the orchestrator. Use `atexit` for graceful shutdown (daemon thread + subprocess reaping on normal exit).

- [ ] **Step 1: Add the wiring block**

In `src/autobot/app.py`, after the `if settings.allow_web:` block (the off-device opt-in tools) and before the `if on_visibility is not None:` block, add:

```python
    if settings.allow_mcp:
        # MCP integration (opt-in, the third disclosed exception). Adds each enabled
        # server's tools to the same registry, gated like any other tool. Network-egress
        # servers send data off-device; that is disclosed per-connection and confirmed
        # by the gate (see PermissionGate). Local stdio servers stay on-device.
        import atexit

        from autobot.mcp.config import load_mcp_config
        from autobot.mcp.manager import McpManager

        mcp_config = load_mcp_config()
        mcp_manager = McpManager(mcp_config, registry)
        mcp_manager.start()
        mcp_manager.connect_enabled()
        atexit.register(mcp_manager.shutdown)
        enabled = sum(1 for c in mcp_config.values() if c.enabled)
        log.info("mcp ENABLED servers=%d enabled=%d", len(mcp_config), enabled)
        print(f"[mcp] MCP enabled — {enabled} of {len(mcp_config)} server(s) connecting.")
```

(No new `build()` parameter and no orchestrator change in this phase; the daemon handle is wired in Phase 4.)

- [ ] **Step 2: Verify import + type + a smoke construction**

Run: `uv run mypy` → `Success`.
Run: `uv run python -c "from autobot.app import build; print('build-import-ok')"` → prints `build-import-ok`.
Run: `uv run pytest -q` → full suite still green (no settings enable `allow_mcp` by default, so this block is dormant in tests).

- [ ] **Step 3: Commit**

```bash
git add src/autobot/app.py
git commit -m "feat(mcp): wire McpManager into the composition root behind allow_mcp"
```

---

### Task 6: End-to-end integration test (stdio echo server)

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/echo_mcp_server.py`
- Create: `tests/integration/test_mcp_integration.py`

**Interfaces:** Consumes the whole stack (`McpManager` → `McpServerWorker` → registry). Produces proof the bridge works against a real MCP server over stdio.

**Context:** `make check` runs `uv run pytest` **without** the `mcp` extra, so this test uses `pytest.importorskip("mcp")` and is skipped there; run it explicitly with `uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v`. `pyproject` already sets `testpaths=["tests"]`, so the new folder is discovered.

- [ ] **Step 1: Create the package marker**

Create `tests/integration/__init__.py` (empty file).

- [ ] **Step 2: Create the echo server fixture**

Create `tests/integration/echo_mcp_server.py`:

```python
"""A tiny FastMCP stdio server with one echo tool — a test fixture only."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-test")


@mcp.tool()
def echo(text: str) -> str:
    """Return the input prefixed with 'echo: '."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
```

- [ ] **Step 3: Write the integration test**

Create `tests/integration/test_mcp_integration.py`:

```python
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

from autobot.mcp.config import McpServerConfig  # noqa: E402
from autobot.mcp.manager import McpManager  # noqa: E402
from autobot.tools.registry import ToolRegistry  # noqa: E402

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
        id="echo", label="Echo", transport="stdio",
        command=sys.executable, args=(_SERVER,), enabled=True, egress="local",
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
```

- [ ] **Step 4: Run the integration test (with the extra)**

Run: `uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v`
Expected: PASS (1) — connects, registers `echo__echo`, the call returns `echo: hello`, and the tool is gone after shutdown.

- [ ] **Step 5: Confirm it skips cleanly in the base gate**

Run: `uv run pytest tests/integration/ -v`
Expected: SKIPPED (mcp not in the base env) — no failure.

- [ ] **Step 6: Full gate**

Run: `make check`
Expected: PASS (the integration test is skipped here; everything else green).

- [ ] **Step 7: Commit**

```bash
git add tests/integration/__init__.py tests/integration/echo_mcp_server.py tests/integration/test_mcp_integration.py
git commit -m "test(mcp): end-to-end stdio echo server integration test"
```

---

## Self-Review

**Spec coverage** (design §13 P2: "session.py worker + manager.py + app.py wiring + gate egress rule + kind=network + [mcp] logging + mcp.* mypy override; local stdio auth:none; list_changed resync; shutdown + subprocess reaping"):
- session.py worker → Task 3 ✓ (connect/list/register/call/resync/shutdown, lazy SDK import, errors→raise).
- manager.py → Task 4 ✓ (loop thread, connect/disconnect/shutdown/status, graceful degradation).
- app.py wiring → Task 5 ✓ (`allow_mcp` block, atexit shutdown).
- gate egress rule + kind → Task 2 ✓ (`risk>=threshold OR (network and risk>=WRITE)`, kind network/danger/write).
- `[mcp]` logging → Tasks 3,4 ✓ (connect/disconnect/synced/loop seams).
- `mcp.*` mypy override → Task 1 ✓.
- local stdio + list_changed resync + shutdown reaping → Tasks 3,6 ✓ (resync via message_handler; reaping via context-manager exit on shutdown; integration test proves connect→call→shutdown→unregister).
- Deferred-from-P1 item: readOnlyHint on network servers — RESOLVED by design: `network` flag (always set for egress servers) drives the ↗ badge regardless of risk, and the per-tool override is the trusted control; `readOnlyHint→READ_ONLY` is kept so network reads don't pop a card (matches mockups). No adapter change. (The other deferred item — design-doc `risk_for` signature wording — is a doc note, addressed in the Phase-2 design reconciliation, not code.)

**Placeholder scan:** none — every code step is complete and runnable; every run step states the command and expected result.

**Type consistency:** `McpServerWorker(config, registry, *, loop, on_event=None)`, `submit_call(tool, args)->str`, `request_shutdown()`, `state`/`tool_count` props, `run()` — identical across Task 3's interface, the manager's use in Task 4, and the integration test. `McpManager(config, registry, *, on_event=None)` + `start/connect_enabled/connect/disconnect/shutdown/status` consistent across Tasks 4,5,6. `tool_allowed(name, allow, deny)` consistent. `_confirm_kind(spec)->str` returns one of network/danger/write. `adapter.*` and `McpServerConfig` field names match Phase-1 exactly.

No issues found.
