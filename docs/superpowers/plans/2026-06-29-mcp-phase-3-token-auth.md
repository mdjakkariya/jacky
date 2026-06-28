# MCP Phase 3 — Token Auth + Keychain + Slack Catalog

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add static-token authentication for MCP servers — a pure `auth.py` module that injects Keychain-stored tokens into a stdio server's environment, wired into the existing `session.py` worker. Also thread-safe the `ToolRegistry` (needed now that the MCP resync loop mutates it while the engine thread reads), extend the daemon's secret allowlist to accept the `mcp.*` namespace, and add a Slack catalog reference doc so the first real bot-token integration can be smoke-tested end-to-end.

**Architecture:** All four deliverables are additive and independent: `auth.py` is a pure function tested against fake `get_secret`; the session wiring is a one-line change in `run()`; the registry lock is a drop-in guard that preserves identical observable behavior; the daemon allowlist change is a helper predicate. The integration test extension (env-var probe tool) proves the full token-injection path against the existing echo server without touching any real Keychain.

**Tech Stack:** Python 3.11, `threading.Lock`, `dataclasses`, `pytest`, mypy strict, ruff. The `mcp` SDK is still opt-in; the integration tests remain `uv run --extra mcp pytest tests/integration/`.

## Global Constraints

- **Python ≥ 3.11**, `from __future__ import annotations` in every module.
- **mypy runs in `strict` mode over BOTH `src` and `tests`** — all new code, including tests, must be fully typed (`-> None` on tests, typed fixtures).
- **Google-style docstrings** on every public module, class, and function (ruff pydocstyle `D`); **tests are exempt** from `D`.
- **Line length 100.** Do not hand-format — run `uv run ruff format .`.
- Value objects are `frozen=True, slots=True` dataclasses with no business logic.
- **On-device only.** `get_secret` is the macOS Keychain (pure on-device). Token injection is local; the token travels off-device only if the MCP server itself sends it, which is the disclosed, gated exception already handled in Phase 2's `network=True` + confirm rule.
- **The `mcp` SDK is an opt-in extra.** `auth.py` must NOT import it. Integration tests use `pytest.importorskip("mcp")` and run via `uv run --extra mcp pytest tests/integration/`; base `make check` (no extra) must stay green.
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- **Verification gate per task:** `make check` green (ruff + ruff-format + mypy + pytest). For integration-touching tasks also: `uv run --extra mcp pytest tests/integration/ -v` green.
- **Branch:** continue on `feat/mcp-integration`. All Phase-3 commits stack there.

**Interfaces produced by Phases 1 + 2 (consume these — already on the branch):**
- `autobot.mcp.config.McpServerConfig` — frozen dataclass with: `id`, `auth_type: str`, `token_env: str | None`, `secret_ref: str | None`, `env: dict[str, str]`, `transport: str`, `command: str | None`, `args: tuple`, `egress: str`, etc.
- `autobot.secrets.get_secret(name: str, runner: Runner | None = None) -> str | None` — Keychain lookup, returns `None` if unset/unavailable.
- `autobot.mcp.session.McpServerWorker.run()` — the injection point: currently builds `env=dict(self._cfg.env) or None` inline before `StdioServerParameters(...)`.
- `autobot.tools.registry.ToolRegistry` — plain `dict[str, ToolSpec]` accessed by `register`, `unregister`, `get`, `schemas`, `dispatch`.
- `autobot.daemon.server._SECRET_NAMES = ("anthropic_api_key", "web_api_key")` — checked by `post_secret`.

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/mcp/auth.py` (create) | Pure `stdio_env_for(cfg, get_secret) -> dict[str, str] \| None` |
| `src/autobot/mcp/session.py` (modify) | Replace inline env build with `stdio_env_for(...)` call in `run()` |
| `src/autobot/tools/registry.py` (modify) | Add `threading.Lock` to guard dict accesses; handler still runs outside lock |
| `src/autobot/daemon/server.py` (modify) | `_is_allowed_secret(name)` helper; `post_secret` uses it to accept `mcp.*` namespace |
| `tests/unit/test_mcp_auth.py` (create) | Unit tests: all branches of `stdio_env_for` with fake `get_secret` |
| `tests/unit/test_tools.py` (modify) | Add concurrent register/dispatch stress test; existing tests still pass |
| `tests/unit/test_daemon_server.py` (modify) | `mcp.slack.token` accepted; arbitrary disallowed name still rejected |
| `tests/integration/echo_mcp_server.py` (modify) | Add `whoami()` tool that returns `os.environ.get("ECHO_TOKEN", "")` |
| `tests/integration/test_mcp_integration.py` (modify) | Add test: configure `env={"ECHO_TOKEN": "sekret"}`, call `whoami`, assert returns `"sekret"` |
| `docs/mcp-servers.md` (create) | Slack `servers.json` entry + manual smoke-test steps (design reference, not tracking) |

---

### Task 1: `src/autobot/mcp/auth.py` — pure token-injection helper

**Files:**
- Create: `src/autobot/mcp/auth.py`
- Create: `tests/unit/test_mcp_auth.py`

**Interfaces:**
- Consumes: `McpServerConfig` (fields: `auth_type`, `secret_ref`, `token_env`, `env`).
- Produces:
  - `stdio_env_for(cfg: McpServerConfig, get_secret: Callable[[str], str | None]) -> dict[str, str] | None`
    — starts from `dict(cfg.env)`; if `cfg.auth_type == "token"` and `cfg.secret_ref` and `cfg.token_env` are both non-`None`, calls `get_secret(cfg.secret_ref)` and, if the result is non-`None`, adds `env[cfg.token_env] = token`. Returns `None` if the resulting dict is empty (so the subprocess inherits the full parent env); otherwise returns the dict.
  - Does **not** import the `mcp` SDK.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mcp_auth.py`:

```python
"""Unit tests for the pure token-injection helper (no Keychain, no SDK)."""

from __future__ import annotations

from collections.abc import Callable

from autobot.mcp.auth import stdio_env_for
from autobot.mcp.config import McpServerConfig


def _cfg(
    *,
    auth_type: str = "none",
    token_env: str | None = None,
    secret_ref: str | None = None,
    env: dict[str, str] | None = None,
) -> McpServerConfig:
    return McpServerConfig(
        id="test",
        label="Test",
        transport="stdio",
        auth_type=auth_type,
        token_env=token_env,
        secret_ref=secret_ref,
        env=env or {},
    )


def _fake_secret(value: str | None) -> Callable[[str], str | None]:
    """A get_secret stub that always returns ``value`` (typed, no ignores needed)."""

    def getter(name: str) -> str | None:
        return value

    return getter


def test_token_injected_when_all_fields_set() -> None:
    cfg = _cfg(auth_type="token", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token",
               env={"SLACK_TEAM_ID": "T0123"})
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123", "SLACK_BOT_TOKEN": "xoxb-fake"}


def test_no_token_when_get_secret_returns_none() -> None:
    cfg = _cfg(auth_type="token", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token",
               env={"SLACK_TEAM_ID": "T0123"})
    result = stdio_env_for(cfg, _fake_secret(None))
    # env is non-empty (SLACK_TEAM_ID), so a dict is returned — but without the token
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_auth_type_none_env_vars_still_returned() -> None:
    cfg = _cfg(auth_type="none", env={"FOO": "bar"})
    result = stdio_env_for(cfg, _fake_secret("ignored"))
    assert result == {"FOO": "bar"}


def test_auth_type_none_ignores_secret_ref() -> None:
    cfg = _cfg(auth_type="none", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token")
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    # auth_type != "token" → secret is never looked up; empty env → None
    assert result is None


def test_token_auth_missing_token_env_skips_injection() -> None:
    # token_env is None → can't inject even if secret is present
    cfg = _cfg(auth_type="token", token_env=None, secret_ref="mcp.slack.token",
               env={"SLACK_TEAM_ID": "T0123"})
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_token_auth_missing_secret_ref_skips_injection() -> None:
    # secret_ref is None → nothing to look up
    cfg = _cfg(auth_type="token", token_env="SLACK_BOT_TOKEN", secret_ref=None,
               env={"SLACK_TEAM_ID": "T0123"})
    result = stdio_env_for(cfg, _fake_secret("xoxb-fake"))
    assert result == {"SLACK_TEAM_ID": "T0123"}


def test_empty_env_and_no_token_returns_none() -> None:
    # Empty cfg.env + auth_type "none" → empty dict → None (inherit parent env)
    cfg = _cfg(auth_type="none")
    assert stdio_env_for(cfg, _fake_secret(None)) is None  # type: ignore[arg-type]


def test_empty_env_with_successful_token_injection_returns_dict() -> None:
    # Even with empty cfg.env, a successful token injection produces a non-empty dict
    cfg = _cfg(auth_type="token", token_env="SLACK_BOT_TOKEN", secret_ref="mcp.slack.token")
    result = stdio_env_for(cfg, _fake_secret("xoxb-token"))
    assert result == {"SLACK_BOT_TOKEN": "xoxb-token"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.mcp.auth`.

- [ ] **Step 3: Write the module**

Create `src/autobot/mcp/auth.py`:

```python
"""Token injection helpers for MCP server connections.

This module is a pure-function layer with no I/O: the Keychain lookup is injected
as a callable, so unit tests run without touching a real Keychain and so different
secret backends can be substituted trivially. It must not import the ``mcp`` SDK —
auth logic is transport-agnostic and must remain importable without the opt-in extra.

Phase 3 supports ``auth_type="token"`` (static bot/API token) for **stdio** servers:
the token is injected as an environment variable in ``StdioServerParameters(env=...)``,
which is the MCP spec's sanctioned path for stdio credential passing. HTTP bearer-header
injection (also ``auth_type="token"``) is added in Phase 6 alongside OAuth2.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.mcp.config import McpServerConfig


def stdio_env_for(
    cfg: McpServerConfig,
    get_secret: Callable[[str], str | None],
) -> dict[str, str] | None:
    """Build the ``env`` dict for a stdio ``StdioServerParameters``.

    Starts from a copy of ``cfg.env`` (the non-secret vars from ``servers.json``).
    When ``cfg.auth_type == "token"`` and both ``cfg.secret_ref`` and
    ``cfg.token_env`` are set, looks up the token via ``get_secret(cfg.secret_ref)``
    and, if non-``None``, adds ``env[cfg.token_env] = token``.

    Returns ``None`` when the resulting dict is empty — ``StdioServerParameters``
    treats ``env=None`` as "inherit the full parent environment", which is the right
    default for unauthenticated local servers and avoids accidentally stripping
    ``PATH`` / ``HOME`` from the subprocess.

    Args:
        cfg: The server's config descriptor (no secrets stored here).
        get_secret: A callable that returns the secret for a Keychain account name,
            or ``None`` if unset/unavailable. Injected so callers can swap the
            real Keychain for a fake in tests.

    Returns:
        A non-empty ``dict[str, str]`` ready for ``StdioServerParameters(env=...)``,
        or ``None`` to signal "inherit parent env".
    """
    env: dict[str, str] = dict(cfg.env)

    if (
        cfg.auth_type == "token"
        and cfg.secret_ref is not None
        and cfg.token_env is not None
    ):
        token = get_secret(cfg.secret_ref)
        if token is not None:
            env[cfg.token_env] = token

    return env if env else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_auth.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Run mypy to confirm strict-clean**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Verify the module imports without the SDK**

Run: `uv run python -c "from autobot.mcp.auth import stdio_env_for; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/mcp/auth.py tests/unit/test_mcp_auth.py
git commit -m "feat(mcp): pure stdio_env_for helper — token injection from Keychain into subprocess env"
```

---

### Task 2: Wire `stdio_env_for` into `session.py` + integration test proof

**Files:**
- Modify: `src/autobot/mcp/session.py` — replace inline env build in `run()`.
- Modify: `tests/integration/echo_mcp_server.py` — add `whoami()` env-probe tool.
- Modify: `tests/integration/test_mcp_integration.py` — add env-injection integration test.

**Interfaces:**
- Consumes: `autobot.mcp.auth.stdio_env_for`, `autobot.secrets.get_secret`.
- Produces: `McpServerWorker.run()` delegates env construction to `stdio_env_for(self._cfg, get_secret)`; the real Keychain `get_secret` is imported at module top (verified below to be lightweight — no heavy deps). The integration test uses `env={"ECHO_TOKEN": "sekret"}` in config (no Keychain needed) and asserts the `whoami` tool returns `"sekret"`, proving env vars reach the subprocess.

**Why `get_secret` at module top (not lazily):** `secrets.py` imports only `collections.abc.Callable` and a `subprocess` (inside a closure). No heavy runtime (torch, MLX, OpenAI) is involved. A quick `uv run python -c "import autobot.secrets; print('ok')"` confirms it is import-light. If this check fails, move the import inside `run()`.

- [ ] **Step 1: Confirm `autobot.secrets` is import-light**

Run: `uv run python -c "import autobot.secrets; print('ok')"`
Expected: prints `ok` immediately (< 0.5 s).

If the import is heavy (slow or fails), move `from autobot.secrets import get_secret` to inside `McpServerWorker.run()` and note the deviation.

- [ ] **Step 2: Wire `stdio_env_for` into `session.py`**

In `src/autobot/mcp/session.py`, add this import at the top of the file (after existing top-level imports, before `if TYPE_CHECKING:`):

```python
from autobot.mcp.auth import stdio_env_for
from autobot.secrets import get_secret as _get_secret
```

Then, in `McpServerWorker.run()`, find and replace this block:

Old (lines ~135–139 in the current file):
```python
            params = StdioServerParameters(
                command=self._cfg.command or "",
                args=list(self._cfg.args),
                env=dict(self._cfg.env) or None,
            )
```

New:
```python
            params = StdioServerParameters(
                command=self._cfg.command or "",
                args=list(self._cfg.args),
                env=stdio_env_for(self._cfg, _get_secret),
            )
```

- [ ] **Step 3: Run mypy to confirm strict-clean**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 4: Run the unit test suite to confirm nothing broke**

Run: `make check`
Expected: PASS.

- [ ] **Step 5: Extend the echo server fixture with a `whoami` env-probe tool**

In `tests/integration/echo_mcp_server.py`, add after the existing `echo` tool:

```python
import os


@mcp.tool()  # type: ignore[misc, unused-ignore]
def whoami() -> str:
    """Return the value of ECHO_TOKEN from the environment (empty string if absent)."""
    return os.environ.get("ECHO_TOKEN", "")
```

Also add `import os` at the top of the file (after `from __future__ import annotations`).

- [ ] **Step 6: Add the integration test for env-variable injection**

In `tests/integration/test_mcp_integration.py`, append a new test function after `test_stdio_echo_connect_call_shutdown`:

```python
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
```

- [ ] **Step 7: Run the integration test**

Run: `uv run --extra mcp pytest tests/integration/test_mcp_integration.py -v`
Expected: PASS (2 passed — existing `test_stdio_echo_connect_call_shutdown` + new `test_stdio_env_var_reaches_subprocess`).

- [ ] **Step 8: Commit**

```bash
git add src/autobot/mcp/session.py tests/integration/echo_mcp_server.py tests/integration/test_mcp_integration.py
git commit -m "feat(mcp): wire stdio_env_for into session worker; prove env injection via integration test"
```

---

### Task 3: `ToolRegistry` thread-safety

**Files:**
- Modify: `src/autobot/tools/registry.py`
- Modify (append): `tests/unit/test_tools.py`

**Interfaces:**
- `ToolRegistry.__init__` gains `self._lock = threading.Lock()`.
- `register(spec, *, replace=False)` — acquire lock, mutate dict, release.
- `unregister(name)` — acquire lock, pop, release.
- `get(name)` — acquire lock, lookup, release, return.
- `schemas()` — acquire lock, snapshot values, release, then iterate snapshot (never holds lock during `to_schema()`).
- `dispatch(name, arguments)` — acquire lock, look up spec, RELEASE lock, THEN call `spec.handler(**args)` outside the lock. This is the critical constraint: handlers may block (MCP call over a `Future`), and holding the lock during handler execution would serialize all tool calls and could deadlock with the MCP worker thread that also calls `register`/`unregister`.
- `default_registry()` — unchanged.

**CRITICAL LOCK SCOPING — `dispatch`:** The lock guards ONLY the dict lookup (finding the spec). The handler is invoked AFTER the lock is released. Any attempt to hold the lock across the handler call violates this constraint.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tools.py`:

```python
import threading
import time


def test_registry_concurrent_register_unregister_does_not_corrupt() -> None:
    """Concurrent mutations and reads from two threads must not raise or corrupt state."""
    registry = ToolRegistry()
    errors: list[Exception] = []
    stop = threading.Event()

    def mutator() -> None:
        i = 0
        while not stop.is_set():
            name = f"stress_{i % 5}"
            try:
                spec = ToolSpec(name=name, description="", parameters={}, handler=lambda: name)
                registry.register(spec, replace=True)
                registry.unregister(name)
            except Exception as exc:
                errors.append(exc)
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                registry.schemas()
                registry.get("stress_0")
            except Exception as exc:
                errors.append(exc)

    t1 = threading.Thread(target=mutator, daemon=True)
    t2 = threading.Thread(target=reader, daemon=True)
    t1.start()
    t2.start()
    time.sleep(0.5)
    stop.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert errors == [], f"thread errors: {errors}"


def test_dispatch_runs_handler_outside_lock() -> None:
    """A handler that takes time must not block concurrent registry reads."""
    import concurrent.futures

    registry = ToolRegistry()
    blocker_started = threading.Event()
    allow_finish = threading.Event()

    def slow_handler() -> str:
        blocker_started.set()
        allow_finish.wait(timeout=2.0)
        return "done"

    registry.register(ToolSpec(name="slow", description="", parameters={}, handler=slow_handler))
    registry.register(ToolSpec(name="fast", description="", parameters={}, handler=lambda: "ok"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        dispatch_future = pool.submit(registry.dispatch, "slow", {})
        blocker_started.wait(timeout=2.0)
        # While slow_handler is running, schemas() and get() must not deadlock
        schema_future = pool.submit(registry.schemas)
        schemas = schema_future.result(timeout=1.0)  # must not hang
        assert any(s["function"]["name"] == "fast" for s in schemas)
        allow_finish.set()
        result = dispatch_future.result(timeout=2.0)
    assert result.ok is True
    assert result.content == "done"
```

- [ ] **Step 2: Run to verify they fail (or reveal the existing race)**

Run: `uv run pytest tests/unit/test_tools.py -k "concurrent or outside_lock" -v`
Expected: either FAIL (race condition hit) or, in CPython with the GIL, the test may pass unreliably — but `test_dispatch_runs_handler_outside_lock` will HANG if the lock is held during handler execution, making the failure deterministic once the implementation has a lock.

- [ ] **Step 3: Implement the lock**

In `src/autobot/tools/registry.py`, add `import threading` at the top (after `from __future__ import annotations`):

```python
import threading
```

In `ToolRegistry.__init__`, add the lock:

```python
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.Lock()
```

Replace `register` with a locked version:

```python
    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        """Add a tool. Raises if the name already exists, unless ``replace`` is set.

        Args:
            spec: The tool to register.
            replace: When ``True``, overwrite an existing tool of the same name
                (used by the MCP manager to re-sync a changed tool definition);
                when ``False`` (default), a duplicate raises.

        Raises:
            ValueError: If ``spec.name`` is already registered and ``replace`` is
                ``False``.
        """
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"tool already registered: {spec.name!r}")
            self._tools[spec.name] = spec
```

Replace `unregister` with a locked version:

```python
    def unregister(self, name: str) -> bool:
        """Remove a registered tool.

        Args:
            name: The tool name to remove.

        Returns:
            ``True`` if a tool was removed, ``False`` if ``name`` was not registered.
        """
        with self._lock:
            return self._tools.pop(name, None) is not None
```

Replace `get` with a locked version:

```python
    def get(self, name: str) -> ToolSpec | None:
        """Return the spec for ``name``, or ``None`` if it is not registered."""
        with self._lock:
            return self._tools.get(name)
```

Replace `schemas` with a locked snapshot + post-lock iteration:

```python
    def schemas(self) -> list[dict[str, Any]]:
        """Return every tool's schema, for advertising to the model."""
        with self._lock:
            specs = list(self._tools.values())
        return [spec.to_schema() for spec in specs]
```

Replace `dispatch` with **lookup-under-lock, handler-outside-lock**:

```python
    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Execute a registered tool by name.

        The dict lookup is guarded by the registry lock; the handler is invoked
        **after** the lock is released. This is intentional and critical: MCP tool
        handlers block on a ``concurrent.futures.Future`` while the MCP worker
        thread (which also calls ``register``/``unregister``) resolves it. Holding
        the lock during handler execution would deadlock those two threads.

        Tool errors are captured and returned as a failed :class:`ToolResult`
        rather than raised, so a misbehaving tool surfaces to the model as a
        message instead of crashing the loop.

        Args:
            name: The tool name requested by the model.
            arguments: JSON-decoded keyword arguments (may be ``None``).

        Returns:
            A :class:`~autobot.core.types.ToolResult`.
        """
        with self._lock:
            spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name=name, content=f"unknown tool: {name!r}", ok=False)
        # Phase 1: insert the permission gate here, keyed on ``spec.risk``.
        try:
            content = spec.handler(**(arguments or {}))
            return ToolResult(name=name, content=content, ok=True)
        except Exception as exc:  # surface any tool error to the model, don't crash
            return ToolResult(name=name, content=f"tool failed: {exc}", ok=False)
```

- [ ] **Step 4: Run all registry tests to verify pass**

Run: `uv run pytest tests/unit/test_tools.py -v`
Expected: PASS — all pre-existing tests pass AND the two new concurrency tests pass. `test_dispatch_runs_handler_outside_lock` must complete without hanging (proves handler runs outside lock).

- [ ] **Step 5: Run mypy**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Add a one-line comment to `manager.py` about `future.cancel()`**

In `src/autobot/mcp/manager.py`, in `disconnect()`, find the `future.cancel()` line and add a comment immediately before it:

```python
                # best-effort: cancel() interrupts a waiting Future but cannot stop a
                # running coroutine; the real shutdown path is the loop.stop() in shutdown().
                future.cancel()
```

- [ ] **Step 7: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/tools/registry.py src/autobot/mcp/manager.py tests/unit/test_tools.py
git commit -m "feat(tools): thread-safe ToolRegistry — lock guards dict, handler runs outside lock"
```

---

### Task 4: Daemon secret allowlist — accept `mcp.*` namespace

**Files:**
- Modify: `src/autobot/daemon/server.py`
- Modify (append): `tests/unit/test_daemon_server.py`

**Interfaces:**
- New module-level helper: `_is_allowed_secret(name: str) -> bool` — returns `True` when `name in _SECRET_NAMES or name.startswith("mcp.")`.
- `post_secret` uses `_is_allowed_secret(name)` instead of `name not in _SECRET_NAMES`.
- `_SECRET_NAMES` tuple itself is **unchanged** (it continues to drive `get_settings`'s `_secrets` map, which reports only the core secrets; MCP secret presence is reported via Phase-4 `/mcp` endpoints).
- Error message in `post_secret` updated to mention the `mcp.*` namespace.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_daemon_server.py`:

```python
def test_post_secret_accepts_mcp_namespace(tmp_path: object) -> None:
    from unittest.mock import patch

    client = _settings_client(tmp_path)
    # Patch set_secret so we don't actually touch the Keychain
    with patch("autobot.secrets.set_secret", return_value=True):
        resp = client.post("/secret", json={"name": "mcp.slack.token", "value": "xoxb-fake"}).json()
    assert resp["ok"] is True


def test_post_secret_accepts_any_mcp_subkey(tmp_path: object) -> None:
    from unittest.mock import patch

    client = _settings_client(tmp_path)
    with patch("autobot.secrets.set_secret", return_value=True):
        resp = client.post(
            "/secret", json={"name": "mcp.github.oauth", "value": "gho-fake"}
        ).json()
    assert resp["ok"] is True


def test_post_secret_still_rejects_arbitrary_names(tmp_path: object) -> None:
    resp = _settings_client(tmp_path).post(
        "/secret", json={"name": "totally_evil", "value": "x"}
    ).json()
    assert resp["ok"] is False
    assert "mcp." in resp["error"]  # error message mentions the mcp namespace


def test_post_secret_rejects_bare_mcp_prefix(tmp_path: object) -> None:
    # "mcp." alone (no sub-key) is not a valid secret name
    resp = _settings_client(tmp_path).post("/secret", json={"name": "mcp.", "value": "x"}).json()
    assert resp["ok"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_daemon_server.py -k "mcp_namespace or mcp_subkey or arbitrary or bare_mcp" -v`
Expected: `test_post_secret_accepts_mcp_namespace` → FAIL (rejected as unknown); `test_post_secret_still_rejects_arbitrary_names` → currently PASS (but error message will change); `test_post_secret_rejects_bare_mcp_prefix` → the behavior we need to specify.

- [ ] **Step 3: Implement `_is_allowed_secret` and update `post_secret`**

In `src/autobot/daemon/server.py`, add the helper immediately after the `_SECRET_NAMES` definition:

```python
def _is_allowed_secret(name: str) -> bool:
    """Whether ``name`` is a permitted Keychain account the Settings view may write.

    Accepts the hard-coded core secrets (API keys for existing providers) AND any
    account under the ``mcp.`` namespace (e.g. ``mcp.slack.token``, ``mcp.gh.oauth``).
    Rejects bare ``"mcp."`` (no sub-key) and arbitrary names.
    """
    if name in _SECRET_NAMES:
        return True
    # e.g. "mcp.slack.token" → prefix "mcp." + at least one char after the dot
    return name.startswith("mcp.") and len(name) > len("mcp.")
```

Then, in `post_secret`, replace:

```python
        if name not in _SECRET_NAMES:
            return {"ok": False, "error": f"unknown secret; allowed: {list(_SECRET_NAMES)}"}
```

With:

```python
        if not _is_allowed_secret(name):
            return {
                "ok": False,
                "error": (
                    f"unknown secret; allowed: {list(_SECRET_NAMES)} or any 'mcp.<id>.*' name"
                ),
            }
```

- [ ] **Step 4: Run all daemon server tests to verify pass**

Run: `uv run pytest tests/unit/test_daemon_server.py -v`
Expected: PASS — all pre-existing tests green AND all four new tests green.

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/daemon/server.py tests/unit/test_daemon_server.py
git commit -m "feat(daemon): extend secret allowlist to accept mcp.* namespace"
```

---

### Task 5: Slack catalog + manual smoke-test reference

**Files:**
- Create: `docs/mcp-servers.md`

**Context:** `CLAUDE.md` says "do not create tracking markdown". This is a *design reference* (how to configure and smoke-test Slack), not a tracking doc — same class as `docs/architecture/design-reference.md`. It documents the standard `servers.json` entry that any user or developer can copy, and the manual steps to verify a Slack bot-token integration end-to-end. No issue numbers, no status checkboxes, no "next steps" language.

- [ ] **Step 1: Create the doc**

Create `docs/mcp-servers.md`:

```markdown
# MCP Server Catalog (Design Reference)

Configuration reference for connecting MCP servers to Jack. Adding a server is
editing `~/.autobot/mcp/servers.json` — never Python code. See
`docs/plans/mcp-integration-design.md` §5 for the full field spec.

## Slack (stdio, bot token)

The `@modelcontextprotocol/server-slack` package runs locally over stdio. It
calls the Slack API using a bot token, so it is a **network-egress** server:
every tool call sends data to Slack. This is the disclosed exception — the
server is opt-in, enabled explicitly, and labelled with a ↗ badge in the UI.

### `servers.json` entry

```jsonc
{
  "servers": {
    "slack": {
      "label": "Slack",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": { "SLACK_TEAM_ID": "T0123456" },
      "auth": { "type": "token" },
      "token_env": "SLACK_BOT_TOKEN",
      "secret_ref": "mcp.slack.token",
      "enabled": false,
      "egress": "network",
      "default_risk": "write",
      "tool_allow": ["slack_*"],
      "tool_risk_overrides": {
        "slack_send_message": "write",
        "slack_schedule_message": "write"
      }
    }
  }
}
```

**Fields:**
- `token_env` — the env-var name the MCP server reads for its bot token.
- `secret_ref` — the Keychain account name where Jack stores the token (never in `servers.json`).
- `egress: "network"` — marks every tool with `network=True`, triggering ↗ badges and gate confirms for writes.
- `enabled: false` — off by default; enable via the Settings view or by setting to `true`.

### Storing the bot token

Store the token in the macOS Keychain once (never on disk):

```bash
# Using autobot's secret helper (after `make run` brings the daemon up):
curl -s -X POST http://127.0.0.1:8765/secret \
  -H "Content-Type: application/json" \
  -d '{"name": "mcp.slack.token", "value": "xoxb-your-real-token"}'

# Or directly via the security CLI:
security add-generic-password -U -s autobot -a mcp.slack.token -w "xoxb-your-real-token"
```

### Manual smoke-test steps

These steps verify the full path: Keychain → token injection → subprocess env → Slack API.

1. **Prerequisites:** Node.js ≥ 18 installed (`node --version`); a Slack bot token with `channels:read`, `chat:write`, `search:read` scopes; `allow_mcp: true` in `~/.autobot/settings.json`.
2. **Write the token** to the Keychain using the `POST /secret` command above.
3. **Enable the server:** set `"enabled": true` in `~/.autobot/mcp/servers.json` and replace `T0123456` with your real workspace Team ID.
4. **Launch Jack:** `make run`. The `[mcp]` log line `mcp connected server=slack tools=N` confirms the connection.
5. **List channels** (read-only, no card): say or type "list my Slack channels". Expect a channel list; check `~/.autobot/logs/autobot.log` for `[mcp]` call logs.
6. **Send a test message** (write, confirm card): say "send 'hello from Jack' to #test-channel". A network confirm card should appear ("Sends data to Slack"); approve it. Verify the message appears in Slack.
7. **Revoke and re-test:** `security delete-generic-password -s autobot -a mcp.slack.token`. Restart Jack. Any Slack tool call should return a failed `ToolResult` (token missing → subprocess auth error), NOT a crash.

### Notes

- The `npx -y` invocation downloads and caches the package on first run; subsequent starts are fast.
- To use the remote Slack-hosted MCP server (OAuth 2.1) instead of the local stdio server, set `"transport": "http"`, `"url": "https://mcp.slack.com/..."`, and `"auth": {"type": "oauth2"}`. The OAuth flow is implemented in Phase 6.
- The `SLACK_TEAM_ID` env var is optional but recommended — it scopes searches to your workspace.
```

- [ ] **Step 2: Verify the file exists and is clean**

Run: `uv run ruff check docs/` (ruff skips `.md` files — expect no output or "no Python files").
Run: `make check`
Expected: PASS (markdown is not linted; the new file has no effect on the Python checks).

- [ ] **Step 3: Commit**

```bash
git add docs/mcp-servers.md
git commit -m "docs(mcp): Slack servers.json entry + manual smoke-test guide"
```

---

## Self-Review

**1. Spec coverage** (design §13 P3: "token auth + Keychain; Slack via bot token end-to-end"):

- `auth.py` pure `stdio_env_for` → Task 1 ✓ (all branches: token injected; get_secret returns None → just env; auth_type != token → secret ignored; empty → None).
- Wire into `session.py` → Task 2 ✓ (`env=stdio_env_for(self._cfg, _get_secret)` replaces inline build).
- Integration proof (env reaches subprocess) → Task 2 ✓ (`whoami` tool + `test_stdio_env_var_reaches_subprocess`).
- Registry thread-safety → Task 3 ✓ (lock guards dict; handler outside lock; stress test + deadlock-proof test).
- `mcp.*` secret allowlist → Task 4 ✓ (`_is_allowed_secret`, tests for accepted/rejected/bare-prefix cases).
- Slack catalog + smoke-test → Task 5 ✓ (`docs/mcp-servers.md` — design reference, not tracking).
- No real Slack creds in automated tests ✓ (integration test uses plain `env={"ECHO_TOKEN": "sekret"}`, no Keychain call).
- Design §7: "for stdio, token injected as env var (StdioServerParameters(env={token_env: value, ...}))" → satisfied by `stdio_env_for` + Task 2 wiring ✓.
- Design §10: "daemon `_SECRET_NAMES` validation extended to accept `mcp.*` namespace" → Task 4 ✓.

**2. Placeholder scan:** No "TBD", "TODO", "handle edge cases", "similar to above", or placeholder code. Every step has a complete, runnable implementation and exact commands with expected output.

**3. Type consistency:**
- `stdio_env_for(cfg: McpServerConfig, get_secret: Callable[[str], str | None]) -> dict[str, str] | None` — identical in Task 1's interfaces block, the module signature, the tests, and the Task 2 call site `stdio_env_for(self._cfg, _get_secret)`.
- `_is_allowed_secret(name: str) -> bool` — consistent between Task 4's interfaces, implementation, and `post_secret` call site.
- `ToolRegistry` lock scope: `dispatch` releases lock before calling `spec.handler` — explicitly stated, implemented, and verified by `test_dispatch_runs_handler_outside_lock` which would hang under a naive "lock the whole method" implementation.
- `McpServerConfig` fields consumed (`auth_type`, `token_env`, `secret_ref`, `env`) match the Phase-1 dataclass definition exactly.

No issues found.
