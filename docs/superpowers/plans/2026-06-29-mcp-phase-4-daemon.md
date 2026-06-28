# MCP Phase 4 — Daemon `/mcp/*` Endpoints + Manager Handle

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the running `McpManager` to the daemon and UI through (a) a `publish_mcp` method on `EventBus` that forwards MCP status/auth events over the WebSocket, (b) a `mcp: McpManager | None` handle on `Orchestrator` set by `build()`, (c) new CRUD methods on `McpManager` to add/remove/enable/disable servers and manage tool overrides with config-persistence + reconnect, (d) a `all_tools` cache on `McpServerWorker` so the UI can display all tools (including disabled ones), and (e) `/mcp/*` HTTP endpoints on the daemon that wrap the manager with a graceful `mcp disabled` fallback.

**Architecture:** All five deliverables stack cleanly on the Phase 2/3 foundation. `EventBus.publish_mcp` is a typed passthrough onto `_emit`. `Orchestrator.mcp` is a plain attribute assigned by `build()` after construction — the orchestrator remains a plain class with no dataclass/slots restrictions. Config mutations in `McpManager` follow a reconnect model: mutate `self._config`, `save_mcp_config`, then `disconnect()` + `connect()` if the server was active — simple, obviously correct, and safe given that CRUD operations are low-frequency user actions. Daemon endpoints follow the existing `create_app` pattern: handlers defined as inner async functions, registered via `app.add_api_route`, and blocking work run via `await asyncio.to_thread(...)`.

**Tech Stack:** Python 3.11, `dataclasses.replace`, `asyncio`, FastAPI `TestClient`, pytest, mypy strict. The `mcp` SDK stays opt-in/lazy; all new unit tests use a `FakeMcp` stub so they run in base `make check`.

## Global Constraints

- **Python ≥ 3.11**, `from __future__ import annotations` in every module.
- **mypy runs in `strict` mode over BOTH `src` and `tests`** — all new code, including tests, must be fully typed (`-> None` on tests, typed fixtures).
- **Google-style docstrings** on every public module, class, and function (ruff pydocstyle `D`); **tests are exempt** from `D`.
- **Line length 100.** Do not hand-format — run `uv run ruff format .`.
- Value objects are `frozen=True, slots=True` dataclasses with no business logic.
- **On-device only.** `POST /mcp/servers` lets a loopback client add a server with an arbitrary stdio `command` that will be spawned on enable. This is equivalent to the user editing `servers.json` directly (loopback, user-driven, same trust level) and is **ACCEPTABLE** for Phase 4. Explicit spawn-consent UI is Phase 6.
- **The `mcp` SDK is an opt-in extra.** All new unit tests use a `FakeMcp` stub and run via base `make check`. Integration-touching tasks also run `uv run --extra mcp pytest tests/integration/ -v`.
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- **Verification gate per task:** `make check` green (ruff + ruff-format + mypy + pytest). For integration-touching tasks also: `uv run --extra mcp pytest tests/integration/ -v` green.
- **Branch:** continue on `feat/mcp-integration`. All Phase-4 commits stack there.

**Interfaces produced by Phases 1–3 (consume these — already on the branch):**
- `autobot.mcp.config`: `McpServerConfig` (frozen dataclass), `load_mcp_config(path)`, `save_mcp_config(servers, path)`, `_coerce_server(server_id, data) -> McpServerConfig | None`, `DEFAULT_MCP_CONFIG_PATH`.
- `autobot.mcp.manager.McpManager(config, registry, *, on_event=None)` with `start/connect_enabled/connect/disconnect/shutdown/status`.
- `autobot.mcp.session.McpServerWorker` with `state`, `tool_count`, `submit_call`, `request_shutdown`, `run()`, `_sync_tools`.
- `autobot.core.events.EventBus._emit(message: dict[str, object])` — private, but used only within `events.py`.
- `autobot.secrets.has_secret(name: str) -> bool`.
- `autobot.daemon.server.create_app(bus, *, settings_path, on_change, on_confirm_answer, on_chat, on_new_session, on_action)` and `run_daemon(bus, host, port, ...)`.
- `autobot.daemon.runner.serve()` builds the orchestrator via `build(...)` and calls `run_daemon(...)`.
- `autobot.app.build(settings, ...)` ends with `return Orchestrator(...)` at ~line 573 of `app.py`.

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/core/events.py` (modify) | Add `publish_mcp(self, payload: dict[str, object]) -> None` |
| `src/autobot/orchestrator/state_machine.py` (modify) | Add `self.mcp: McpManager \| None = None` in `Orchestrator.__init__` |
| `src/autobot/app.py` (modify) | Hoist `mcp_manager`; add `on_mcp_event` param; assign `orch.mcp`; return `orch` |
| `src/autobot/daemon/runner.py` (modify) | Pass `on_mcp_event=bus.publish_mcp` to `build()`; pass `mcp=orchestrator.mcp` to `run_daemon()` |
| `src/autobot/mcp/manager.py` (modify) | Add `config_path` param; add `add_or_update_server`, `remove_server`, `set_enabled`, `tools_for`, `set_tool_override`, `secret_present` |
| `src/autobot/mcp/session.py` (modify) | Cache `self._all_tools: list[dict[str, object]]` in `_sync_tools`; add `all_tools()` method |
| `src/autobot/daemon/server.py` (modify) | Add `mcp` param to `create_app` and `run_daemon`; register `/mcp/*` routes |
| `tests/unit/test_events.py` (modify) | Add `test_publish_mcp_reaches_subscriber` |
| `tests/unit/test_mcp_manager.py` (modify) | Add persistence tests for new CRUD methods |
| `tests/unit/test_daemon_server.py` (modify) | Add `/mcp/*` endpoint tests with `FakeMcp` stub |
| `tests/integration/test_mcp_integration.py` (modify) | Add `all_tools()` cache assertion |

---

### Task 1: `EventBus.publish_mcp` — forward MCP events over the WebSocket

**Files:**
- Modify: `src/autobot/core/events.py`
- Modify: `tests/unit/test_events.py`

**Interfaces:**
- Produces: `EventBus.publish_mcp(self, payload: dict[str, object]) -> None` — calls `self._emit(payload)` directly (no wrapping dataclass needed; the worker already emits the final wire dict).
- Both current event shapes flow through it: `{"type": "mcp_status", "server": id, "state": ..., "tool_count": ...}` (Phase 2) and `{"type": "mcp_oauth", ...}` (Phase 6 stub).
- Does NOT import the `mcp` SDK.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_events.py`:

```python
def test_publish_mcp_reaches_subscriber() -> None:
    bus = EventBus()
    received: list[dict[str, object]] = []
    bus.subscribe(received.append)

    payload: dict[str, object] = {
        "type": "mcp_status",
        "server": "slack",
        "state": "connected",
        "tool_count": 7,
    }
    bus.publish_mcp(payload)

    assert received == [payload]


def test_publish_mcp_passes_payload_unchanged() -> None:
    bus = EventBus()
    received: list[dict[str, object]] = []
    bus.subscribe(received.append)

    # Any dict shape flows through — publish_mcp is a typed passthrough
    oauth_payload: dict[str, object] = {"type": "mcp_oauth", "server": "github", "url": "https://x"}
    bus.publish_mcp(oauth_payload)

    assert len(received) == 1
    assert received[0]["type"] == "mcp_oauth"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_events.py -k "publish_mcp" -v`
Expected: FAIL — `AttributeError: 'EventBus' object has no attribute 'publish_mcp'`.

- [ ] **Step 3: Add `publish_mcp` to `EventBus`**

In `src/autobot/core/events.py`, add this method to `EventBus` after `publish_workspace`:

```python
    def publish_mcp(self, payload: dict[str, object]) -> None:
        """Forward an MCP status or auth event to all WebSocket clients.

        A typed passthrough onto :meth:`_emit`. The worker already builds the
        final wire dict (``{"type": "mcp_status", ...}`` for state changes,
        ``{"type": "mcp_oauth", ...}`` for Phase 6 auth flows) — this method
        just routes it through the fan-out without modification.

        Args:
            payload: The wire dict produced by :class:`~autobot.mcp.session.McpServerWorker`
                or the OAuth handler; must contain at least ``{"type": str}``.
        """
        self._emit(payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: PASS (all pre-existing tests + 2 new ones).

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/core/events.py tests/unit/test_events.py
git commit -m "feat(events): add publish_mcp — typed passthrough for MCP status/auth events"
```

---

### Task 2: Expose `McpManager` handle on `Orchestrator` + wire in `build()` and `runner.py`

**Files:**
- Modify: `src/autobot/orchestrator/state_machine.py`
- Modify: `src/autobot/app.py`
- Modify: `src/autobot/daemon/runner.py`

**Interfaces:**
- `Orchestrator.__init__` gains `self.mcp: McpManager | None = None` (plain attribute, `TYPE_CHECKING` import of `McpManager`).
- `build(settings, ..., on_mcp_event: Callable[[dict[str, object]], None] | None = None)` — new optional param added after existing `on_step`.
- Inside `build()`: init `mcp_manager: McpManager | None = None` before the `if settings.allow_mcp:` block; pass `on_event=on_mcp_event` to `McpManager(...)` (replacing the missing `on_event` it currently has — actually the current `build()` does NOT pass `on_event`, so this adds it); after `orch = Orchestrator(...)`, set `orch.mcp = mcp_manager` if not None; return `orch`.
- `runner.serve()`: pass `on_mcp_event=bus.publish_mcp` into `build(...)`; after build returns, pass `mcp=orchestrator.mcp` into `run_daemon(...)`.
- `create_app` and `run_daemon` grow an `mcp: McpManager | None = None` param (threaded through — the actual endpoint logic is Task 5).

**Rationale for `TYPE_CHECKING` import:** `McpManager` is a heavy module (asyncio, threading). Adding it to `Orchestrator.__init__`'s runtime imports would slow every import path that touches the orchestrator. `TYPE_CHECKING` keeps it annotation-only; mypy sees the type, the runtime sees `None`.

- [ ] **Step 1: Add `mcp` attribute to `Orchestrator.__init__`**

In `src/autobot/orchestrator/state_machine.py`, add to the existing `TYPE_CHECKING` block (or create one):

```python
if TYPE_CHECKING:
    from autobot.mcp.manager import McpManager
```

Then in `Orchestrator.__init__`, at the end of the assignments (after `self._dismissed = False`), add:

```python
        # Set by the composition root (app.build) when MCP is enabled.
        # Exposed so the daemon can delegate /mcp/* requests to the live manager.
        self.mcp: McpManager | None = None
```

- [ ] **Step 2: Refactor `build()` to hoist `mcp_manager` and add `on_mcp_event` param**

In `src/autobot/app.py`, locate the `build(` signature (it currently ends around `on_step: Callable[...] | None = None`). Add `on_mcp_event` at the end:

```python
def build(
    settings: Settings | None = None,
    ...
    on_step: Callable[[int, str, str, str], None] | None = None,
    on_mcp_event: Callable[[dict[str, object]], None] | None = None,
) -> Orchestrator:
```

Then, inside `build()`, find the `if settings.allow_mcp:` block. Before it, init:

```python
    mcp_manager: McpManager | None = None
```

Inside the block, change the local variable assignment so it sets `mcp_manager` (not a new local name). Ensure `McpManager(mcp_config, registry, on_event=on_mcp_event, ...)` receives the `on_event` argument. The `McpManager` constructor currently takes `on_event` as a keyword arg — pass `on_mcp_event` to it.

Then replace the final `return Orchestrator(...)` with:

```python
    orch = Orchestrator(
        settings=settings,
        audio=audio,
        stt=stt,
        llm=llm,
        gate=gate,
        wake_gate=_build_wake_gate(settings),
        tts=tts,
        transcript=transcript,
        on_state=on_state or _print_transition,
        memory=memory,
        on_context=on_context,
        on_step=on_step,
        on_show=(lambda: on_visibility(True)) if on_visibility is not None else None,
        release_voice_io=_voice_io.release,
    )
    if mcp_manager is not None:
        orch.mcp = mcp_manager
    return orch
```

- [ ] **Step 3: Add `mcp` param stubs to `create_app` and `run_daemon`**

In `src/autobot/daemon/server.py`, add `mcp: Any | None = None` to both `create_app(...)` and `run_daemon(...)` signatures. Thread it: `run_daemon` passes `mcp=mcp` into the `create_app(...)` call. The parameter is accepted but not yet used inside `create_app` (endpoint logic is Task 5).

For mypy, use `Any` for now (Task 5 will tighten to `McpManager | None` under `TYPE_CHECKING`).

- [ ] **Step 4: Wire `runner.serve()` to pass `on_mcp_event` and `mcp`**

In `src/autobot/daemon/runner.py`, update the `build(...)` call to pass `on_mcp_event=bus.publish_mcp`. Then update the `run_daemon(...)` call to pass `mcp=orchestrator.mcp`.

- [ ] **Step 5: Run mypy + unit tests**

Run: `uv run mypy`
Expected: `Success: no issues found`.

Run: `uv run pytest -q`
Expected: PASS (all existing tests; no new tests here — the wiring is verified end-to-end in Task 5).

- [ ] **Step 6: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autobot/orchestrator/state_machine.py src/autobot/app.py src/autobot/daemon/server.py src/autobot/daemon/runner.py
git commit -m "feat(mcp): expose McpManager handle on Orchestrator; wire on_mcp_event + mcp through build/runner"
```

---

### Task 3: `McpManager` CRUD methods + `config_path` persistence

**Files:**
- Modify: `src/autobot/mcp/manager.py`
- Modify: `tests/unit/test_mcp_manager.py`

**Interfaces:**

New `McpManager.__init__` param (keyword-only, after `on_event`):
```python
config_path: str | Path = DEFAULT_MCP_CONFIG_PATH
```

New public methods (all synchronous; persist + reconnect):

```python
def add_or_update_server(self, descriptor: dict[str, object]) -> dict[str, object]:
    """Validate, persist, and (re)connect a server descriptor.

    Args:
        descriptor: A raw JSON object matching the servers.json schema (must include
            ``"id"`` and a valid ``"transport"``). Validated via ``_coerce_server``.

    Returns:
        ``{"ok": True, "server": status_row}`` on success, or
        ``{"ok": False, "error": str}`` if the descriptor is invalid.
    """

def remove_server(self, server_id: str) -> bool:
    """Disconnect (if running) and remove a server from config + disk.

    Returns:
        ``True`` if the server existed and was removed, ``False`` if unknown.
    """

def set_enabled(self, server_id: str, enabled: bool) -> bool:
    """Toggle a server's ``enabled`` flag, persist, and reconnect if needed.

    Returns:
        ``True`` if the server was found, ``False`` if unknown.
    """

def tools_for(self, server_id: str) -> list[dict[str, object]]:
    """Return the cached all-tools list for ``server_id`` (empty if not connected).

    Returns:
        A copy of the worker's ``_all_tools`` list (from :meth:`McpServerWorker.all_tools`).
    """

def set_tool_override(
    self,
    server_id: str,
    tool: str,
    *,
    risk: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Adjust a tool's risk classification or enable/disable it, persist + reconnect.

    - ``enabled=False``: adds ``tool`` to ``cfg.tool_deny`` (replaces frozen tuple).
    - ``enabled=True``: removes ``tool`` from ``cfg.tool_deny``.
    - ``risk`` (e.g. ``"read_only"``, ``"write"``, ``"destructive"``): sets/clears
      ``cfg.tool_risk_overrides[tool]``.

    Returns:
        ``True`` if the server was found, ``False`` if unknown.
    """

def secret_present(self, server_id: str) -> bool:
    """Whether the Keychain secret referenced by ``server_id``'s config is set.

    Returns:
        ``True`` if ``cfg.secret_ref`` is non-None and ``has_secret(cfg.secret_ref)``
        returns ``True``; ``False`` otherwise.
    """
```

**Config-change reconciliation rationale:** `add_or_update_server` / `set_enabled` / `set_tool_override` all follow the same pattern: mutate `self._config` (replacing the frozen `McpServerConfig` via `dataclasses.replace`), call `save_mcp_config(self._config, self._config_path)`, then if the server had an active worker: `self.disconnect(server_id)` + `self.connect(server_id)` so the fresh worker picks up the new frozen config. This is simpler and obviously correct compared to mutating a live worker's config mid-flight; user-action frequency makes the reconnect cost negligible.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_mcp_manager.py`:

```python
import dataclasses
import json
from pathlib import Path


def _write_servers_json(tmp_path: Path, servers: dict[str, object]) -> Path:
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return p


def test_add_or_update_server_persists_new_server(tmp_path: Path) -> None:
    p = _write_servers_json(tmp_path, {})
    mgr = McpManager({}, ToolRegistry(), config_path=p)

    result = mgr.add_or_update_server({
        "id": "echo",
        "label": "Echo",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "echo_server"],
        "enabled": False,
    })

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
    servers = {
        "echo": {"transport": "stdio", "command": "python", "enabled": False}
    }
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
    servers = {
        "echo": {"transport": "stdio", "command": "python", "enabled": False}
    }
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
    servers = {
        "echo": {"transport": "stdio", "command": "python", "enabled": False}
    }
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config
    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    ok = mgr.set_tool_override("echo", "echo__dangerous", enabled=False)

    assert ok is True
    saved = json.loads(p.read_text())
    assert "echo__dangerous" in saved["servers"]["echo"]["tool_deny"]


def test_set_tool_override_risk_persists(tmp_path: Path) -> None:
    servers = {
        "echo": {"transport": "stdio", "command": "python", "enabled": False}
    }
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config
    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)

    ok = mgr.set_tool_override("echo", "echo__read", risk="read_only")

    assert ok is True
    saved = json.loads(p.read_text())
    assert saved["servers"]["echo"]["tool_risk_overrides"]["echo__read"] == "read_only"


def test_secret_present_returns_false_when_no_secret_ref(tmp_path: Path) -> None:
    servers = {
        "echo": {"transport": "stdio", "command": "python", "enabled": False}
    }
    p = _write_servers_json(tmp_path, servers)
    from autobot.mcp.config import load_mcp_config
    cfg = load_mcp_config(p)
    mgr = McpManager(cfg, ToolRegistry(), config_path=p)
    assert mgr.secret_present("echo") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_manager.py -k "add_or_update or remove_server or set_enabled or set_tool or secret_present" -v`
Expected: FAIL — `TypeError: McpManager.__init__() got an unexpected keyword argument 'config_path'` (and similar for the new methods).

- [ ] **Step 3: Implement the new methods**

In `src/autobot/mcp/manager.py`, add `DEFAULT_MCP_CONFIG_PATH` to the `TYPE_CHECKING` import block and import it at module top (it's a string constant, not heavy):

```python
from autobot.mcp.config import DEFAULT_MCP_CONFIG_PATH
```

Update `McpManager.__init__` to accept and store `config_path`:

```python
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
```

**First, extend `status()`** to include `auth_type` in each row, so the daemon's list
endpoint gets it from the public status payload rather than reaching into private config.
In `McpManager.status()`, add `"auth_type": cfg.auth_type,` to the row dict (the loop
already has `cfg` in scope):

```python
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
```

(The existing `test_status_lists_all_configured_servers` only reads the `"server"` key,
so adding `auth_type` is backward-compatible.)

Then add the new methods after `status()`:

```python
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
            return {"ok": False, "error": f"invalid transport or descriptor for server {server_id!r}"}
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
```

Note: `Path` must be imported at the top of `manager.py`. Add `from pathlib import Path` if not already present.

- [ ] **Step 4: Run the new tests + full suite**

Run: `uv run pytest tests/unit/test_mcp_manager.py -v`
Expected: PASS — all pre-existing tests + all 9 new tests.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autobot/mcp/manager.py tests/unit/test_mcp_manager.py
git commit -m "feat(mcp): McpManager CRUD — add/remove/enable/disable servers + tool overrides + persistence"
```

---

### Task 4: `McpServerWorker.all_tools()` — cache full tool list before filtering

**Files:**
- Modify: `src/autobot/mcp/session.py`
- Modify (optional): `tests/integration/test_mcp_integration.py`

**Interfaces:**
- `McpServerWorker.__init__` gains `self._all_tools: list[dict[str, object]] = []`.
- `_sync_tools` builds `self._all_tools` (the full pre-filter snapshot) before the deny/allow filtering loop.
- New public method: `all_tools(self) -> list[dict[str, object]]` — returns `list(self._all_tools)` (GIL-safe copy, same pattern as `state`/`tool_count`).

Each entry in `_all_tools`:
```python
{
    "name": tool.name,           # bare tool name (without namespace)
    "description": tool.description or "",
    "risk": adapter.risk_for(tool, floor=floor, overrides=overrides).name.lower(),
    "network": network,
    "enabled": tool_allowed(tool.name, self._cfg.tool_allow, self._cfg.tool_deny),
}
```

This pre-filter snapshot lets the UI show all tools (including denied ones) with their effective risk and `enabled` flag, so a user can toggle them.

- [ ] **Step 1: Add `_all_tools` attribute to `__init__`**

In `src/autobot/mcp/session.py`, in `McpServerWorker.__init__`, after `self._tool_count = 0`, add:

```python
        # Full snapshot of the server's tool list (built before allow/deny filtering).
        # Lets the UI show disabled tools and their risk; the registry only gets the
        # filtered subset. GIL-safe reads via the all_tools() copy method.
        self._all_tools: list[dict[str, object]] = []
```

- [ ] **Step 2: Build the snapshot in `_sync_tools` before filtering**

In `McpServerWorker._sync_tools`, after computing `floor`, `overrides`, and `network` but before the `desired` dict construction, add:

```python
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
```

- [ ] **Step 3: Add the `all_tools()` public method**

In `McpServerWorker`, after the `tool_count` property, add:

```python
    def all_tools(self) -> list[dict[str, object]]:
        """Return a copy of the full pre-filter tool snapshot.

        Includes tools that are excluded by ``tool_deny`` / ``tool_allow`` (with
        ``enabled=False``), so the UI can show and toggle them. The list is rebuilt
        on every ``_sync_tools`` call (connect and ``tools/list_changed`` resync).

        Returns:
            A copy of ``_all_tools``; each item is ``{name, description, risk, network, enabled}``.
        """
        return list(self._all_tools)
```

- [ ] **Step 4: Run mypy**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Extend the integration test to assert `all_tools()` is populated**

In `tests/integration/test_mcp_integration.py`, in `test_stdio_echo_connect_call_shutdown`, after the `registry.dispatch(...)` assertion, add:

```python
        # all_tools() returns the full pre-filter snapshot including the whoami tool
        worker = manager._workers.get("echo")
        if worker is not None:
            snapshot = worker.all_tools()
            names = [t["name"] for t in snapshot]
            assert "echo" in names
            assert all(isinstance(t["enabled"], bool) for t in snapshot)
```

- [ ] **Step 6: Run integration tests**

Run: `uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v`
Expected: PASS.

- [ ] **Step 7: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/mcp/session.py tests/integration/test_mcp_integration.py
git commit -m "feat(mcp): worker caches full all_tools() snapshot before allow/deny filtering"
```

---

### Task 5: Daemon `/mcp/*` endpoints + `FakeMcp` tests

**Files:**
- Modify: `src/autobot/daemon/server.py`
- Modify: `tests/unit/test_daemon_server.py`

**Interfaces:**

Replace the `mcp: Any | None = None` stub from Task 2 with `mcp: McpManager | None = None` under `TYPE_CHECKING`. The `mcp` param flows from `run_daemon` → `create_app` → handler closures.

Endpoints (all inside `create_app`):

| Method | Path | Action | `mcp is None` response |
|--------|------|--------|------------------------|
| `GET` | `/mcp/servers` | `mcp.status()` + `auth_type`, `secret_present` per row | `{"ok": False, "error": "mcp disabled"}` |
| `POST` | `/mcp/servers` | body = descriptor; `mcp.add_or_update_server(body)` | same |
| `DELETE` | `/mcp/servers/{id}` | `mcp.remove_server(id)` | same |
| `POST` | `/mcp/servers/{id}/enable` | `mcp.set_enabled(id, True)` | same |
| `POST` | `/mcp/servers/{id}/disable` | `mcp.set_enabled(id, False)` | same |
| `POST` | `/mcp/servers/{id}/connect` | `mcp.connect(id)` → return `{"ok": True}` | same |
| `POST` | `/mcp/servers/{id}/test` | `mcp.connect(id)` → return first matching status row | same |
| `GET` | `/mcp/servers/{id}/tools` | `mcp.tools_for(id)` | same |
| `POST` | `/mcp/servers/{id}/tools/{tool}` | body `{risk?, enabled?}` → `mcp.set_tool_override(id, tool, ...)` | same |
| `POST` | `/mcp/servers/{id}/auth/start` | Phase-6 stub: `{"ok": False, "error": "oauth not yet supported (phase 6)"}` | same |

All blocking `mcp.*` calls run via `await asyncio.to_thread(fn, ...)` to keep the event loop responsive.

The `GET /mcp/servers` response uses the `status()` rows (which already include
`auth_type` after the Task-3 change) and enriches each with:
- `"secret_present"`: `mcp.secret_present(row["server"])`

The handler must NOT reach into `mcp._config` (the `_FakeMcp` test stub has no such
attribute, and poking private state is the wrong contract) — `auth_type` comes from
the public `status()` row.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_daemon_server.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: /mcp/* endpoint tests with a FakeMcp stub
# ---------------------------------------------------------------------------

from typing import Any


class _FakeMcp:
    """Minimal McpManager stub for daemon endpoint tests (no SDK, no subprocess)."""

    def __init__(self) -> None:
        self._servers: dict[str, dict[str, Any]] = {
            "echo": {
                "server": "echo", "label": "Echo", "enabled": True,
                "egress": "local", "state": "connected", "tool_count": 2,
                "auth_type": "none", "secret_ref": None,
            }
        }

    def status(self) -> list[dict[str, Any]]:
        return list(self._servers.values())

    def secret_present(self, server_id: str) -> bool:
        cfg = self._servers.get(server_id, {})
        return cfg.get("secret_ref") is not None

    def add_or_update_server(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        sid = descriptor.get("id", "")
        transport = descriptor.get("transport", "")
        if transport not in {"stdio", "http"}:
            return {"ok": False, "error": "invalid transport"}
        self._servers[str(sid)] = {"server": sid, "label": sid, "enabled": False,
                                   "egress": "local", "state": "disconnected",
                                   "tool_count": 0, "auth_type": "none", "secret_ref": None}
        return {"ok": True, "server": self._servers[str(sid)]}

    def remove_server(self, server_id: str) -> bool:
        return self._servers.pop(server_id, None) is not None

    def set_enabled(self, server_id: str, enabled: bool) -> bool:
        if server_id not in self._servers:
            return False
        self._servers[server_id]["enabled"] = enabled
        return True

    def connect(self, server_id: str) -> None:
        if server_id in self._servers:
            self._servers[server_id]["state"] = "connected"

    def tools_for(self, server_id: str) -> list[dict[str, Any]]:
        if server_id not in self._servers:
            return []
        return [
            {"name": "echo", "description": "Echo text", "risk": "read_only",
             "network": False, "enabled": True},
            {"name": "whoami", "description": "Return token", "risk": "read_only",
             "network": False, "enabled": True},
        ]

    def set_tool_override(
        self, server_id: str, tool: str, *, risk: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        return server_id in self._servers


def _mcp_client(mcp: _FakeMcp) -> TestClient:
    bus = EventBus()
    return TestClient(create_app(bus, mcp=mcp))


def test_mcp_list_returns_servers() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is True
    servers = resp["servers"]
    assert len(servers) == 1
    assert servers[0]["server"] == "echo"
    assert "auth_type" in servers[0]
    assert "secret_present" in servers[0]


def test_mcp_list_when_disabled_returns_error() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))  # no mcp= → disabled
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is False
    assert "mcp disabled" in resp["error"]


def test_mcp_add_valid_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.post("/mcp/servers", json={
        "id": "gh", "label": "GitHub", "transport": "stdio",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
        "enabled": False,
    }).json()
    assert resp["ok"] is True
    assert "gh" in fake._servers


def test_mcp_add_invalid_descriptor() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers", json={"id": "bad", "transport": "grpc"}).json()
    assert resp["ok"] is False


def test_mcp_delete_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.delete("/mcp/servers/echo").json()
    assert resp["ok"] is True
    assert "echo" not in fake._servers


def test_mcp_delete_unknown_server() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.delete("/mcp/servers/nonexistent").json()
    assert resp["ok"] is False


def test_mcp_enable_server() -> None:
    fake = _FakeMcp()
    fake._servers["echo"]["enabled"] = False
    client = _mcp_client(fake)
    resp = client.post("/mcp/servers/echo/enable").json()
    assert resp["ok"] is True
    assert fake._servers["echo"]["enabled"] is True


def test_mcp_disable_server() -> None:
    fake = _FakeMcp()
    client = _mcp_client(fake)
    resp = client.post("/mcp/servers/echo/disable").json()
    assert resp["ok"] is True
    assert fake._servers["echo"]["enabled"] is False


def test_mcp_connect_server() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/connect").json()
    assert resp["ok"] is True


def test_mcp_test_server_returns_status_row() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/test").json()
    assert resp["ok"] is True
    assert resp["server"]["server"] == "echo"


def test_mcp_get_tools() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers/echo/tools").json()
    assert resp["ok"] is True
    assert len(resp["tools"]) == 2
    assert resp["tools"][0]["name"] == "echo"


def test_mcp_set_tool_override() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post(
        "/mcp/servers/echo/tools/echo",
        json={"risk": "write", "enabled": False},
    ).json()
    assert resp["ok"] is True


def test_mcp_auth_start_returns_phase6_stub() -> None:
    client = _mcp_client(_FakeMcp())
    resp = client.post("/mcp/servers/echo/auth/start").json()
    assert resp["ok"] is False
    assert "phase 6" in resp["error"]


def test_mcp_disabled_all_endpoints_return_error() -> None:
    bus = EventBus()
    client = TestClient(create_app(bus))  # no mcp
    for method, path in [
        ("POST", "/mcp/servers"),
        ("DELETE", "/mcp/servers/x"),
        ("POST", "/mcp/servers/x/enable"),
        ("POST", "/mcp/servers/x/disable"),
        ("POST", "/mcp/servers/x/connect"),
        ("POST", "/mcp/servers/x/test"),
        ("GET", "/mcp/servers/x/tools"),
        ("POST", "/mcp/servers/x/tools/t"),
        ("POST", "/mcp/servers/x/auth/start"),
    ]:
        if method == "GET":
            resp = client.get(path).json()
        else:
            resp = client.request(method, path, json={}).json()
        assert resp["ok"] is False, f"{method} {path} should return ok=False when mcp disabled"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_daemon_server.py -k "mcp_list or mcp_add or mcp_delete or mcp_enable or mcp_disable or mcp_connect or mcp_test or mcp_get_tools or mcp_set_tool or mcp_auth or mcp_disabled" -v`
Expected: FAIL — `create_app` doesn't accept `mcp=` yet (or the routes don't exist).

- [ ] **Step 3: Implement `/mcp/*` routes in `create_app`**

In `src/autobot/daemon/server.py`, update the `create_app` signature to tighten the `mcp` type (under `TYPE_CHECKING`):

```python
if TYPE_CHECKING:
    from autobot.mcp.manager import McpManager
```

Update the signature:
```python
def create_app(
    bus: EventBus,
    settings_path: str | Path | None = None,
    on_change: Any | None = None,
    on_confirm_answer: Any | None = None,
    on_chat: Any | None = None,
    on_new_session: Any | None = None,
    on_action: Any | None = None,
    mcp: McpManager | None = None,
) -> Any:
```

Then, before the `app.add_api_route(...)` registration block, add all the `/mcp/*` handler functions:

```python
    # ------------------------------------------------------------------ MCP
    # All /mcp/* handlers check mcp is not None and return a graceful error
    # when MCP is disabled (allow_mcp=False). Blocking manager calls are run
    # via asyncio.to_thread so the event loop stays responsive.

    _MCP_DISABLED: dict[str, object] = {"ok": False, "error": "mcp disabled"}

    async def get_mcp_servers() -> dict[str, Any]:
        """List all configured MCP servers with status + auth metadata."""
        if mcp is None:
            return _MCP_DISABLED
        rows = await asyncio.to_thread(mcp.status)
        # status() rows already carry auth_type; enrich each with secret_present
        # (a Keychain lookup) via the public method — no reaching into mcp internals.
        enriched: list[dict[str, Any]] = [
            {**row, "secret_present": await asyncio.to_thread(mcp.secret_present, str(row["server"]))}
            for row in rows
        ]
        return {"ok": True, "servers": enriched}

    async def post_mcp_servers(request: Request) -> dict[str, Any]:
        """Add or update an MCP server (validated via _coerce_server)."""
        if mcp is None:
            return _MCP_DISABLED
        body = await request.json()
        if not isinstance(body, dict):
            return {"ok": False, "error": "expected a JSON object"}
        return await asyncio.to_thread(mcp.add_or_update_server, body)

    async def delete_mcp_server(server_id: str) -> dict[str, Any]:
        """Remove a configured MCP server."""
        if mcp is None:
            return _MCP_DISABLED
        removed = await asyncio.to_thread(mcp.remove_server, server_id)
        if not removed:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        return {"ok": True}

    async def post_mcp_enable(server_id: str) -> dict[str, Any]:
        """Enable an MCP server (persist + connect)."""
        if mcp is None:
            return _MCP_DISABLED
        ok = await asyncio.to_thread(mcp.set_enabled, server_id, True)
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_disable(server_id: str) -> dict[str, Any]:
        """Disable an MCP server (persist + disconnect)."""
        if mcp is None:
            return _MCP_DISABLED
        ok = await asyncio.to_thread(mcp.set_enabled, server_id, False)
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_connect(server_id: str) -> dict[str, Any]:
        """Connect to an MCP server (start worker if not running)."""
        if mcp is None:
            return _MCP_DISABLED
        await asyncio.to_thread(mcp.connect, server_id)
        return {"ok": True}

    async def post_mcp_test(server_id: str) -> dict[str, Any]:
        """Connect to an MCP server and return its current status row."""
        if mcp is None:
            return _MCP_DISABLED
        await asyncio.to_thread(mcp.connect, server_id)
        rows = await asyncio.to_thread(mcp.status)
        match = next((r for r in rows if r["server"] == server_id), None)
        if match is None:
            return {"ok": False, "error": f"unknown server: {server_id!r}"}
        return {"ok": True, "server": match}

    async def get_mcp_tools(server_id: str) -> dict[str, Any]:
        """Return the cached all-tools list for a server."""
        if mcp is None:
            return _MCP_DISABLED
        tools = await asyncio.to_thread(mcp.tools_for, server_id)
        return {"ok": True, "tools": tools}

    async def post_mcp_tool_override(server_id: str, tool: str, request: Request) -> dict[str, Any]:
        """Adjust a tool's risk classification or enable/disable it."""
        if mcp is None:
            return _MCP_DISABLED
        body = await request.json()
        risk: str | None = body.get("risk") if isinstance(body, dict) else None
        enabled: bool | None = body.get("enabled") if isinstance(body, dict) else None
        ok = await asyncio.to_thread(
            mcp.set_tool_override, server_id, tool, risk=risk, enabled=enabled
        )
        return {"ok": ok} if ok else {"ok": False, "error": f"unknown server: {server_id!r}"}

    async def post_mcp_auth_start(server_id: str) -> dict[str, Any]:
        """OAuth start — stub until Phase 6."""
        if mcp is None:
            return _MCP_DISABLED
        return {"ok": False, "error": "oauth not yet supported (phase 6)"}
```

Then add the route registrations before `return app`:

```python
    app.add_api_route("/mcp/servers", get_mcp_servers, methods=["GET"])
    app.add_api_route("/mcp/servers", post_mcp_servers, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}", delete_mcp_server, methods=["DELETE"])
    app.add_api_route("/mcp/servers/{server_id}/enable", post_mcp_enable, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/disable", post_mcp_disable, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/connect", post_mcp_connect, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/test", post_mcp_test, methods=["POST"])
    app.add_api_route("/mcp/servers/{server_id}/tools", get_mcp_tools, methods=["GET"])
    app.add_api_route(
        "/mcp/servers/{server_id}/tools/{tool}", post_mcp_tool_override, methods=["POST"]
    )
    app.add_api_route(
        "/mcp/servers/{server_id}/auth/start", post_mcp_auth_start, methods=["POST"]
    )
```

Also update `run_daemon` to thread `mcp` through:

```python
    app = create_app(
        bus,
        on_change=on_change,
        on_confirm_answer=on_confirm_answer,
        on_chat=on_chat,
        on_new_session=on_new_session,
        on_action=on_action,
        mcp=mcp,
    )
```

- [ ] **Step 4: Run all daemon server tests**

Run: `uv run pytest tests/unit/test_daemon_server.py -v`
Expected: PASS — all pre-existing tests + all new MCP endpoint tests.

- [ ] **Step 5: Run mypy**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autobot/daemon/server.py tests/unit/test_daemon_server.py
git commit -m "feat(daemon): /mcp/* endpoints — list/add/delete/enable/disable/connect/test/tools/auth stub"
```

---

### Task 6: Secret-presence enrichment + SIGTERM subprocess reaping smoke-check

**Files:**
- Modify: `src/autobot/daemon/server.py` (minor — the `secret_present` call in `get_mcp_servers` was done in Task 5; this task verifies correctness)
- Modify: `tests/unit/test_daemon_server.py` (add secret-presence assertions)

**Interfaces:**
- `GET /mcp/servers` response: each row has `"secret_present": bool` based on `McpManager.secret_present(server_id)`.
- SIGTERM handling: the existing `atexit.register(mcp_manager.shutdown)` in `app.py` is sufficient — uvicorn calls `atexit` on `SIGTERM`. No new code needed; this task adds a unit-level assertion that `shutdown()` is safe to call after `start()` (already tested in Task 3 — this is a documentation note).

- [ ] **Step 1: Add secret-presence test cases**

Append to `tests/unit/test_daemon_server.py`:

```python
def test_mcp_list_includes_secret_present_false_when_no_ref() -> None:
    """Servers without a secret_ref report secret_present=False."""
    client = _mcp_client(_FakeMcp())
    resp = client.get("/mcp/servers").json()
    assert resp["ok"] is True
    echo = next(s for s in resp["servers"] if s["server"] == "echo")
    assert echo["secret_present"] is False


def test_mcp_list_includes_secret_present_true_when_ref_set() -> None:
    """Servers with a secret_ref and a set Keychain entry report secret_present=True."""
    from unittest.mock import patch

    fake = _FakeMcp()
    fake._servers["echo"]["secret_ref"] = "mcp.echo.token"

    class _FakeMcpWithRef(_FakeMcp):
        def secret_present(self, server_id: str) -> bool:
            cfg_data = self._servers.get(server_id, {})
            return cfg_data.get("secret_ref") is not None

    client = _mcp_client(_FakeMcpWithRef())
    resp = client.get("/mcp/servers").json()
    echo = next(s for s in resp["servers"] if s["server"] == "echo")
    assert echo["secret_present"] is True
```

- [ ] **Step 2: Make daemon shutdown reap the manager deterministically**

`atexit.register(mcp_manager.shutdown)` (in `app.py`) already covers graceful exits — uvicorn's SIGTERM handler exits cleanly, which runs `atexit`. But now that the `mcp` handle is passed to `run_daemon`, add an explicit `finally` so the manager is shut down deterministically when the server loop ends (before any other atexit handlers, and not dependent on atexit ordering). `McpManager.shutdown()` is idempotent, so the redundant atexit call is harmless.

In `src/autobot/daemon/server.py`, in `run_daemon`, wrap the uvicorn run so shutdown always reaps:

```python
    try:
        with contextlib.suppress(KeyboardInterrupt):
            uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    finally:
        if mcp is not None:
            mcp.shutdown()
    print("\n[daemon] stopped.")
```

(Leave the existing `atexit.register` in `app.py` in place — both firing is safe because `shutdown()` is idempotent. This covers SIGINT, SIGTERM-graceful, and the normal-return path; only `SIGKILL`/`os._exit` can't be reaped, which is unavoidable.)

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/test_daemon_server.py -v`
Expected: PASS.

- [ ] **Step 4: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 5: Integration smoke**

Run: `uv run --extra mcp pytest tests/integration/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/daemon/server.py tests/unit/test_daemon_server.py
git commit -m "feat(daemon): reap MCP manager on daemon shutdown; assert secret_present enrichment"
```

---

## Self-Review

**1. Spec coverage** (design §10 Phase 4: "daemon /mcp/* HTTP endpoints + WS events + manager handle"):

- `EventBus.publish_mcp` → Task 1 ✓ (typed passthrough onto `_emit`; both `mcp_status` and `mcp_oauth` shapes flow through; unit-tested).
- `Orchestrator.mcp` attribute → Task 2 ✓ (`TYPE_CHECKING` import, plain assignment after `Orchestrator(...)`, set by `build()` if `allow_mcp`).
- `build()` `on_mcp_event` param + `mcp_manager` hoisting → Task 2 ✓ (hoist before `allow_mcp` block, pass `on_event=on_mcp_event` to `McpManager`, set `orch.mcp = mcp_manager` before return).
- `runner.serve()` wiring → Task 2 ✓ (`on_mcp_event=bus.publish_mcp` into `build`; `mcp=orchestrator.mcp` into `run_daemon`).
- `McpManager.config_path` + persistence → Task 3 ✓ (`config_path: str | Path = DEFAULT_MCP_CONFIG_PATH`; all CRUD methods call `save_mcp_config`).
- `add_or_update_server` → Task 3 ✓ (validates via `_coerce_server`; returns `{"ok": False, "error": ...}` on bad transport; persist + reconnect).
- `remove_server` → Task 3 ✓ (disconnect if active, delete from `_config`, persist, idempotent).
- `set_enabled` → Task 3 ✓ (`dataclasses.replace(cfg, enabled=...)`, persist, reconnect if enabled).
- `tools_for` → Task 3 ✓ (delegates to `worker.all_tools()`, returns `[]` if not connected).
- `set_tool_override` → Task 3 ✓ (deny tuple mutation + risk overrides dict, persist + reconnect).
- `secret_present` → Task 3 ✓ (`has_secret(cfg.secret_ref)` or `False` if no ref).
- Worker `_all_tools` cache → Task 4 ✓ (built in `_sync_tools` before filtering; `all_tools()` returns a copy; integration test asserts it).
- `/mcp/servers` GET → Task 5 ✓ (status rows + `auth_type` + `secret_present`).
- `/mcp/servers` POST → Task 5 ✓ (`add_or_update_server`; invalid descriptor → `ok=False`).
- `/mcp/servers/{id}` DELETE → Task 5 ✓ (`remove_server`; unknown → `ok=False`).
- `/mcp/servers/{id}/enable` + `/disable` → Task 5 ✓ (`set_enabled`).
- `/mcp/servers/{id}/connect` + `/test` → Task 5 ✓ (`connect`; test returns status row).
- `/mcp/servers/{id}/tools` GET → Task 5 ✓ (`tools_for`).
- `/mcp/servers/{id}/tools/{tool}` POST → Task 5 ✓ (`set_tool_override`).
- `/mcp/servers/{id}/auth/start` → Task 5 ✓ (Phase-6 stub: `{"ok": False, "error": "oauth not yet supported (phase 6)"}`).
- `mcp is None` graceful path → Task 5 ✓ (all endpoints return `{"ok": False, "error": "mcp disabled"}`; tested with a dedicated assertion loop).
- Secret-presence enrichment → Task 6 ✓ (assertions for `False` and `True` cases).
- SIGTERM subprocess reaping → Task 6 ✓ (covered by existing `atexit.register(mcp_manager.shutdown)` in `app.py`; no new code required).

**2. Placeholder scan:** No "TBD", "TODO", "handle edge cases", "similar to above", or placeholder code. Every step has a complete, runnable implementation and exact commands with expected output.

**3. Type consistency:**
- `EventBus.publish_mcp(payload: dict[str, object]) -> None` — consistent in Task 1 interface, implementation, tests, and `runner.py` usage (`on_mcp_event=bus.publish_mcp`).
- `McpManager(config, registry, *, on_event=None, config_path=DEFAULT_MCP_CONFIG_PATH)` — consistent across Task 3 interface block, implementation, tests (using `config_path=p`), and `app.py` wiring.
- `add_or_update_server(descriptor: dict[str, object]) -> dict[str, object]` — used as `dict[str, Any]` in implementation (equivalent under mypy); consistent across interface, stub, and route handler.
- `tools_for(server_id: str) -> list[dict[str, object]]` — consistent with `worker.all_tools() -> list[dict[str, object]]` (Task 4) and the GET endpoint response `{"ok": True, "tools": list}`.
- `set_tool_override(server_id, tool, *, risk=None, enabled=None) -> bool` — consistent across interface, implementation, `FakeMcp`, and route handler.
- `Orchestrator.mcp: McpManager | None` — `TYPE_CHECKING` import so no runtime cost; set unconditionally to `None` in `__init__`, optionally overwritten by `build()`.
- `create_app(..., mcp: McpManager | None = None)` and `run_daemon(..., mcp: McpManager | None = None)` — consistent signatures, `mcp` threaded through both without intermediate casts.

**4. Security note recorded:** `POST /mcp/servers` lets a loopback client add a server with an arbitrary `command`. This is loopback-only, user-driven (same trust as editing `servers.json`), and is accepted for Phase 4. Explicit spawn-consent UI is Phase 6. Stated in Global Constraints.

No issues found.
