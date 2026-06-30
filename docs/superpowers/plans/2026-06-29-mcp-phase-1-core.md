# MCP Phase 1 — Pure Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SDK-free foundation for MCP integration — the `ToolSpec.network` field, registry `unregister`/idempotent-replace, the `allow_mcp` setting, the `mcp` opt-in extra, and the pure `autobot.mcp.adapter` + `autobot.mcp.config` modules — all unit-tested, with no runtime, no network, and no UI.

**Architecture:** Everything here is pure logic that later phases consume. The adapter operates on MCP tool/result shapes via minimal structural `Protocol`s, so it never imports the `mcp` SDK and its tests run with plain fakes. The config module load/saves `~/.autobot/mcp/servers.json` exactly like `autobot.config` does for `settings.json`. The two existing-file changes (`ToolSpec.network`, registry `unregister`) are additive and inert until phase 2 wires the gate and manager.

**Tech Stack:** Python 3.11, dataclasses, `hashlib`/`json`, pytest, mypy strict, ruff. The `mcp` Python SDK is declared as an opt-in extra but **not imported** by any phase-1 code.

## Global Constraints

- **Python ≥ 3.11**, `from __future__ import annotations` in every module.
- **mypy runs in `strict` mode over BOTH `src` and `tests`** — all new code, including tests, must be fully typed (`-> None` on tests, typed fixtures).
- **Google-style docstrings** on every public module, class, and function (ruff pydocstyle `D`); **tests are exempt** from `D`.
- **Line length 100.** Do not hand-format — run `uv run ruff format .`.
- Value objects are `frozen=True, slots=True` dataclasses with no business logic.
- **The `mcp` SDK must not be imported by phase-1 code.** The adapter uses structural `Protocol`s; importing `autobot.mcp.adapter`/`autobot.mcp.config` must work with the SDK absent.
- **SDK pin (for the extra only): `mcp>=1.28,<2`** (v2 is alpha and changes the OAuth callback signature).
- **Commit messages: Conventional Commits** (`feat:`, `chore:`, `test:`, …). **No `Co-Authored-By` trailer.**
- **Verification gate:** `make check` (ruff check + ruff format --check + mypy + pytest) must pass before a task is done.
- Run a single test file with `uv run pytest tests/unit/<file>.py -v`.

**Branch:** create `feat/mcp-phase-1-core` off `main` before Task 1 (`git checkout main && git checkout -b feat/mcp-phase-1-core`). All task commits land there; the PR closes the phase-1 issue.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` (modify) | Declare the `mcp` opt-in extra `["mcp>=1.28,<2"]` |
| `src/autobot/mcp/__init__.py` (create) | Package marker + module docstring; **no SDK import** |
| `src/autobot/tools/registry.py` (modify) | Add `ToolSpec.network: bool`; add `ToolRegistry.unregister()`; add `register(spec, *, replace=False)` |
| `src/autobot/config.py` (modify) | Add `Settings.allow_mcp: bool = False` |
| `src/autobot/mcp/adapter.py` (create) | Pure: schema map, result→text, risk classification, fingerprint, namespacing, risk-name parse |
| `src/autobot/mcp/config.py` (create) | `McpServerConfig` dataclass + `load_mcp_config`/`save_mcp_config` |
| `tests/unit/test_tools.py` (modify) | Tests for `ToolSpec.network`, `unregister`, `register(replace=)` |
| `tests/unit/test_config.py` (modify) | Test for `allow_mcp` |
| `tests/unit/test_mcp_adapter.py` (create) | Tests for every adapter function |
| `tests/unit/test_mcp_config.py` (create) | Tests for config load/save round-trip + robustness |

---

### Task 1: `mcp` opt-in extra + package skeleton

**Files:**
- Modify: `pyproject.toml` (the `[project.optional-dependencies]` block)
- Create: `src/autobot/mcp/__init__.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the `autobot.mcp` package (so later tasks can add `adapter.py`/`config.py`); the `mcp` extra (`uv sync --extra mcp`) used by phase 2.

- [ ] **Step 1: Create the package marker**

Create `src/autobot/mcp/__init__.py`:

```python
"""MCP integration: connect to MCP servers and expose their tools as ``ToolSpec``s.

This subpackage is the *only* place the ``mcp`` SDK is used, and it is imported
lazily (per the repo's "import heavy runtimes lazily" rule) inside the manager /
session modules added in later phases. The pure layers (``adapter``, ``config``)
import no SDK at all, so they — and their tests — stay fast and dependency-free.
"""

from __future__ import annotations
```

- [ ] **Step 2: Declare the opt-in extra**

In `pyproject.toml`, inside `[project.optional-dependencies]`, add this block immediately after the `whispercpp = [...]` block:

```toml
# MCP integration (opt-in, off by default). The official MCP client SDK; lazy-
# imported by autobot.mcp and only needed when allow_mcp is set. `uv sync --extra mcp`.
# Pinned below 2.0 — v2 is alpha and changes the OAuth callback signature.
mcp = [
    "mcp>=1.28,<2",
]
```

- [ ] **Step 3: Verify the package imports without the SDK**

Run: `uv run python -c "import autobot.mcp; print('ok')"`
Expected: prints `ok` (no `mcp` SDK required).

- [ ] **Step 4: Verify the extra resolves**

Run: `uv sync --extra mcp`
Expected: completes without a resolution error (confirms the `mcp>=1.28,<2` pin is installable on Python 3.11).

- [ ] **Step 5: Verify the gate is still green**

Run: `make check`
Expected: PASS (ruff, ruff-format, mypy, pytest all green).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/autobot/mcp/__init__.py
git commit -m "chore(mcp): add opt-in mcp extra and mcp package skeleton"
```

---

### Task 2: `ToolSpec.network` field

**Files:**
- Modify: `src/autobot/tools/registry.py` (the `ToolSpec` dataclass)
- Test: `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ToolSpec(..., network: bool = False)` — a new keyword field. Phase 2's gate reads `spec.network`; the adapter (Task 5) sets it for network-egress tools.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tools.py`:

```python
def test_toolspec_network_defaults_false() -> None:
    spec = ToolSpec(name="t", description="", parameters={}, handler=lambda: "")
    assert spec.network is False


def test_toolspec_network_can_be_set() -> None:
    spec = ToolSpec(
        name="t", description="", parameters={}, handler=lambda: "", network=True
    )
    assert spec.network is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tools.py -k network -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'network'` / `AttributeError`.

- [ ] **Step 3: Add the field**

In `src/autobot/tools/registry.py`, in the `ToolSpec` dataclass, add this field immediately after the `requires: str | None = None` field (keep it last so positional construction is unaffected):

```python
    # True when this tool sends user data off the device (a network-egress MCP
    # tool). Drives the UI's "↗ sends data off-device" badge and the audit egress
    # note, and — for WRITE-or-higher tools — makes the gate confirm even below the
    # destructive threshold (see PermissionGate, phase 2). False for all local tools.
    network: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tools.py -k network -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/registry.py tests/unit/test_tools.py
git commit -m "feat(tools): add ToolSpec.network flag for off-device egress tools"
```

---

### Task 3: `ToolRegistry.unregister` + idempotent `register(replace=)`

**Files:**
- Modify: `src/autobot/tools/registry.py` (the `ToolRegistry` class)
- Test: `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ToolRegistry.unregister(name: str) -> bool` — removes a tool; returns `True` if it existed, `False` otherwise.
  - `ToolRegistry.register(spec: ToolSpec, *, replace: bool = False) -> None` — unchanged default (raises `ValueError` on duplicate); `replace=True` overwrites. Phase 2's manager uses both for `tools/list_changed` resync and enable/disable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tools.py`:

```python
def _spec(name: str, desc: str = "") -> ToolSpec:
    return ToolSpec(name=name, description=desc, parameters={}, handler=lambda: name)


def test_register_duplicate_still_raises_by_default() -> None:
    registry = ToolRegistry()
    registry.register(_spec("dup"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("dup"))


def test_register_replace_overwrites_existing() -> None:
    registry = ToolRegistry()
    registry.register(_spec("t", "old"))
    registry.register(_spec("t", "new"), replace=True)
    spec = registry.get("t")
    assert spec is not None
    assert spec.description == "new"


def test_unregister_removes_tool_and_reports_existed() -> None:
    registry = ToolRegistry()
    registry.register(_spec("gone"))
    assert registry.unregister("gone") is True
    assert registry.get("gone") is None
    assert "gone" not in [s["function"]["name"] for s in registry.schemas()]


def test_unregister_missing_tool_returns_false() -> None:
    assert ToolRegistry().unregister("never") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tools.py -k "register or unregister" -v`
Expected: FAIL (`unregister` undefined; `register()` has no `replace` keyword).

- [ ] **Step 3: Update `register` and add `unregister`**

In `src/autobot/tools/registry.py`, replace the existing `register` method with:

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
        if spec.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> bool:
        """Remove a registered tool.

        Args:
            name: The tool name to remove.

        Returns:
            ``True`` if a tool was removed, ``False`` if ``name`` was not registered.
            Used when an MCP server is disabled or a tool disappears on re-sync.
        """
        return self._tools.pop(name, None) is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tools.py -k "register or unregister" -v`
Expected: PASS (4 passed). Then `uv run pytest tests/unit/test_tools.py -v` — all existing registry tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/registry.py tests/unit/test_tools.py
git commit -m "feat(tools): registry unregister + idempotent register(replace=) for MCP resync"
```

---

### Task 4: `Settings.allow_mcp` flag

**Files:**
- Modify: `src/autobot/config.py` (the `Settings` dataclass)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.allow_mcp: bool = False` — the master gate phase 2 reads in `app.py::build()` (mirrors `allow_web`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_allow_mcp_defaults_off() -> None:
    assert Settings().allow_mcp is False


def test_allow_mcp_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"allow_mcp": True}, path)
    assert Settings.load(path).allow_mcp is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k allow_mcp -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'allow_mcp'`.

- [ ] **Step 3: Add the field**

In `src/autobot/config.py`, in the `Settings` dataclass, add this block immediately after the web-search settings (right after the `web_backend: str = ...` line, before the `# --- daemon (Phase 3c) ---` comment):

```python
    # --- MCP integration (opt-in, off-device; the third disclosed exception) ---
    # Master gate for the whole MCP subsystem, mirroring allow_web. Off by default;
    # individual servers are still each opt-in via ~/.autobot/mcp/servers.json.
    allow_mcp: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -k allow_mcp -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/config.py tests/unit/test_config.py
git commit -m "feat(config): add allow_mcp master flag (off by default)"
```

---

### Task 5: `autobot.mcp.adapter` — pure adapters

**Files:**
- Create: `src/autobot/mcp/adapter.py`
- Test: `tests/unit/test_mcp_adapter.py`

**Interfaces:**
- Consumes: `autobot.core.types.Risk`.
- Produces (all consumed by phase 2's manager/session):
  - `namespaced(server_id: str, tool_name: str) -> str`
  - `split_namespaced(name: str) -> tuple[str, str] | None`
  - `params_from_input_schema(input_schema: Mapping[str, Any] | None) -> dict[str, Any]`
  - `result_to_text(result: _ResultLike) -> tuple[str, bool]`
  - `risk_for(tool: _ToolLike, *, floor: Risk, overrides: Mapping[str, Risk]) -> Risk`
  - `risk_from_name(name: str | None, default: Risk = Risk.WRITE) -> Risk`
  - `fingerprint(tool: _ToolLike) -> str`
  - Structural protocols `_ToolLike`, `_ResultLike` (the SDK's `Tool`/`CallToolResult` satisfy them).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mcp_adapter.py`:

```python
"""Tests for the pure MCP adapters (no SDK, no network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from autobot.core.types import Risk
from autobot.mcp import adapter


@dataclass
class FakeAnnotations:
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None


@dataclass
class FakeTool:
    name: str
    description: str | None = None
    inputSchema: dict[str, Any] = field(default_factory=dict)
    annotations: Any = None


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    mimeType: str = ""
    uri: str = ""
    resource: Any = None


@dataclass
class FakeResult:
    content: list[Any]
    isError: bool = False


def test_namespacing_roundtrip() -> None:
    assert adapter.namespaced("slack", "send_message") == "slack__send_message"
    assert adapter.split_namespaced("slack__send_message") == ("slack", "send_message")


def test_split_namespaced_rejects_unnamespaced() -> None:
    assert adapter.split_namespaced("plain") is None
    assert adapter.split_namespaced("__x") is None
    assert adapter.split_namespaced("x__") is None


def test_params_passthrough_and_empty_default() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert adapter.params_from_input_schema(schema) == schema
    assert adapter.params_from_input_schema(None) == {"type": "object", "properties": {}}
    assert adapter.params_from_input_schema({}) == {"type": "object", "properties": {}}


def test_result_joins_text_blocks() -> None:
    r = FakeResult(content=[FakeBlock("text", text="hello"), FakeBlock("text", text="world")])
    assert adapter.result_to_text(r) == ("hello\nworld", False)


def test_result_flags_error() -> None:
    r = FakeResult(content=[FakeBlock("text", text="boom")], isError=True)
    assert adapter.result_to_text(r) == ("boom", True)


def test_result_renders_non_text_placeholders() -> None:
    r = FakeResult(content=[FakeBlock("image", mimeType="image/png")])
    text, is_error = adapter.result_to_text(r)
    assert text == "[image image/png]"
    assert is_error is False


def test_result_empty_is_placeholder() -> None:
    assert adapter.result_to_text(FakeResult(content=[])) == ("(no content)", False)


def test_risk_override_wins() -> None:
    tool = FakeTool(name="send", annotations=FakeAnnotations(readOnlyHint=True))
    assert adapter.risk_for(
        tool, floor=Risk.WRITE, overrides={"send": Risk.DESTRUCTIVE}
    ) is Risk.DESTRUCTIVE


def test_risk_destructive_hint_maps_destructive() -> None:
    tool = FakeTool(name="rm", annotations=FakeAnnotations(destructiveHint=True))
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.DESTRUCTIVE


def test_risk_readonly_hint_maps_read_only() -> None:
    tool = FakeTool(name="search", annotations=FakeAnnotations(readOnlyHint=True))
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.READ_ONLY


def test_risk_no_hint_falls_to_floor() -> None:
    tool = FakeTool(name="post")  # no annotations
    assert adapter.risk_for(tool, floor=Risk.WRITE, overrides={}) is Risk.WRITE


def test_risk_from_name() -> None:
    assert adapter.risk_from_name("read") is Risk.READ_ONLY
    assert adapter.risk_from_name("write") is Risk.WRITE
    assert adapter.risk_from_name("destructive") is Risk.DESTRUCTIVE
    assert adapter.risk_from_name(None) is Risk.WRITE
    assert adapter.risk_from_name("nonsense") is Risk.WRITE


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = FakeTool(name="t", description="d", inputSchema={"type": "object"})
    b = FakeTool(name="t", description="d", inputSchema={"type": "object"})
    c = FakeTool(name="t", description="CHANGED", inputSchema={"type": "object"})
    assert adapter.fingerprint(a) == adapter.fingerprint(b)
    assert adapter.fingerprint(a) != adapter.fingerprint(c)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_adapter.py -v`
Expected: FAIL at import (`ModuleNotFoundError: autobot.mcp.adapter`).

- [ ] **Step 3: Write the module**

Create `src/autobot/mcp/adapter.py`:

```python
"""Pure adapters: MCP tool/result shapes → autobot's tool vocabulary.

No MCP SDK import lives here. Inputs are described by minimal structural
``Protocol``s, so this module — and its tests — stay SDK-free and import-light,
matching the repo's "pure logic is unit-tested without the runtime" pattern. The
session worker (added later) passes the SDK's real ``Tool`` / ``CallToolResult``
objects, which satisfy these protocols structurally.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from autobot.core.types import Risk


class _ToolLike(Protocol):
    """Structural view of an MCP ``Tool`` as returned by ``list_tools()``."""

    name: str
    description: str | None
    inputSchema: dict[str, Any]
    annotations: Any  # an annotations object or None; duck-typed to avoid union friction


class _ResultLike(Protocol):
    """Structural view of an MCP ``CallToolResult``."""

    content: Sequence[Any]
    isError: bool


_RISK_BY_NAME: dict[str, Risk] = {
    "read": Risk.READ_ONLY,
    "read_only": Risk.READ_ONLY,
    "readonly": Risk.READ_ONLY,
    "write": Risk.WRITE,
    "destructive": Risk.DESTRUCTIVE,
    "danger": Risk.DESTRUCTIVE,
}


def namespaced(server_id: str, tool_name: str) -> str:
    """Return the registry name for a server's tool, e.g. ``slack__send_message``."""
    return f"{server_id}__{tool_name}"


def split_namespaced(name: str) -> tuple[str, str] | None:
    """Split ``<id>__<tool>`` into ``(id, tool)``; ``None`` if not namespaced."""
    server_id, sep, tool = name.partition("__")
    if not sep or not server_id or not tool:
        return None
    return server_id, tool


def params_from_input_schema(input_schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Map an MCP ``inputSchema`` (already JSON Schema) to ``ToolSpec.parameters``.

    Returns an empty object schema when the server omits a schema, so an
    argument-less tool is still advertised with a valid signature.
    """
    if not input_schema:
        return {"type": "object", "properties": {}}
    return dict(input_schema)


def result_to_text(result: _ResultLike) -> tuple[str, bool]:
    """Flatten a ``CallToolResult``'s content blocks to ``(text, is_error)``.

    Non-text blocks render as short placeholders so a tool returning an
    image/resource still yields a usable string. ``is_error`` mirrors the result's
    ``isError`` flag — the caller turns it into a failed ``ToolResult`` rather than
    raising.
    """
    parts: list[str] = []
    for block in result.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(str(getattr(block, "text", "")))
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            parts.append(
                str(text) if text is not None else f"[resource {getattr(res, 'uri', '')}]"
            )
        elif btype == "resource_link":
            parts.append(f"[resource_link {getattr(block, 'uri', '')}]")
        elif btype in ("image", "audio"):
            parts.append(f"[{btype} {getattr(block, 'mimeType', '')}]")
        else:
            parts.append(f"[{btype}]")
    text = "\n".join(p for p in parts if p).strip()
    return (text or "(no content)", bool(result.isError))


def risk_for(tool: _ToolLike, *, floor: Risk, overrides: Mapping[str, Risk]) -> Risk:
    """Classify a tool's :class:`Risk`. **Server annotations are advisory only.**

    Precedence: an explicit per-tool ``overrides`` entry wins; else a destructive
    hint maps to ``DESTRUCTIVE``; else a read-only hint maps to ``READ_ONLY``; else
    the server's ``floor`` (its ``default_risk``, normally ``WRITE``). Hints are
    never trusted to lower risk below the floor except the explicit read-only case.
    """
    if tool.name in overrides:
        return overrides[tool.name]
    ann = tool.annotations
    if ann is not None and bool(getattr(ann, "destructiveHint", False)):
        return Risk.DESTRUCTIVE
    if ann is not None and bool(getattr(ann, "readOnlyHint", False)):
        return Risk.READ_ONLY
    return floor


def risk_from_name(name: str | None, default: Risk = Risk.WRITE) -> Risk:
    """Map a config risk string ("read"/"write"/"destructive") to :class:`Risk`."""
    if not name:
        return default
    return _RISK_BY_NAME.get(name.strip().lower(), default)


def fingerprint(tool: _ToolLike) -> str:
    """Return a stable SHA-256 over a tool's identity-defining fields.

    Covers name, description, input schema, and annotation hints — so a server that
    silently redefines an approved tool ("rug pull") yields a different fingerprint,
    which the manager uses to force re-consent.
    """
    ann = tool.annotations
    ann_dict = (
        None
        if ann is None
        else {
            "readOnlyHint": getattr(ann, "readOnlyHint", None),
            "destructiveHint": getattr(ann, "destructiveHint", None),
            "idempotentHint": getattr(ann, "idempotentHint", None),
            "openWorldHint": getattr(ann, "openWorldHint", None),
        }
    )
    payload = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
        "annotations": ann_dict,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_adapter.py -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Run mypy to confirm strict-clean**

Run: `uv run mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/mcp/adapter.py tests/unit/test_mcp_adapter.py
git commit -m "feat(mcp): pure adapter — schema/result/risk/fingerprint/namespacing"
```

---

### Task 6: `autobot.mcp.config` — server descriptors

**Files:**
- Create: `src/autobot/mcp/config.py`
- Test: `tests/unit/test_mcp_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces (consumed by phase 2's manager + phase 4's daemon endpoints):
  - `McpServerConfig` (frozen dataclass — fields below).
  - `load_mcp_config(path=DEFAULT_MCP_CONFIG_PATH) -> dict[str, McpServerConfig]`
  - `save_mcp_config(servers: dict[str, McpServerConfig], path=DEFAULT_MCP_CONFIG_PATH) -> None`
  - `DEFAULT_MCP_CONFIG_PATH = "~/.autobot/mcp/servers.json"`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mcp_config.py`:

```python
"""Tests for the JSON-backed MCP server descriptors."""

from __future__ import annotations

from pathlib import Path

from autobot.mcp.config import (
    McpServerConfig,
    load_mcp_config,
    save_mcp_config,
)


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_mcp_config(tmp_path / "nope.json") == {}


def test_load_malformed_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_mcp_config(p) == {}


def test_load_parses_a_stdio_server(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text(
        """
        {"servers": {"slack": {
            "label": "Slack", "transport": "stdio", "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-slack"],
            "env": {"SLACK_TEAM_ID": "T0123"},
            "auth": {"type": "token"}, "token_env": "SLACK_BOT_TOKEN",
            "secret_ref": "mcp.slack.token", "enabled": false,
            "egress": "network", "default_risk": "write",
            "tool_allow": ["slack_*"], "tool_risk_overrides": {"slack_send_message": "write"}
        }}}
        """,
        encoding="utf-8",
    )
    servers = load_mcp_config(p)
    assert set(servers) == {"slack"}
    s = servers["slack"]
    assert s.id == "slack"
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ("-y", "@modelcontextprotocol/server-slack")
    assert s.env == {"SLACK_TEAM_ID": "T0123"}
    assert s.auth_type == "token"
    assert s.token_env == "SLACK_BOT_TOKEN"
    assert s.egress == "network"
    assert s.enabled is False
    assert s.tool_allow == ("slack_*",)
    assert s.tool_risk_overrides == {"slack_send_message": "write"}


def test_load_skips_server_with_bad_transport(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    p.write_text('{"servers": {"x": {"transport": "carrier-pigeon"}}}', encoding="utf-8")
    assert load_mcp_config(p) == {}


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "mcp" / "servers.json"
    cfg = McpServerConfig(
        id="gh",
        label="GitHub",
        transport="http",
        url="https://api.githubcopilot.com/mcp/",
        auth_type="oauth2",
        secret_ref="mcp.gh.oauth",
        enabled=True,
        egress="network",
        default_risk="write",
        tool_allow=("repo_*",),
        tool_risk_overrides={"create_issue": "write"},
    )
    save_mcp_config({"gh": cfg}, p)
    assert p.exists()
    reloaded = load_mcp_config(p)
    assert reloaded == {"gh": cfg}


def test_save_sets_owner_only_perms(tmp_path: Path) -> None:
    p = tmp_path / "servers.json"
    save_mcp_config({}, p)
    assert (p.stat().st_mode & 0o777) == 0o600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_config.py -v`
Expected: FAIL at import (`ModuleNotFoundError: autobot.mcp.config`).

- [ ] **Step 3: Write the module**

Create `src/autobot/mcp/config.py`:

```python
"""Declarative MCP server descriptors, persisted as JSON (config only, no secrets).

Mirrors the ``settings.json`` split: this file holds connection config; the
Keychain holds tokens (account names like ``mcp.<id>.token``). Adding a server is
editing ``~/.autobot/mcp/servers.json`` (or using the Settings view) — never code.
Robust by design: a missing or malformed file yields ``{}`` and a server with an
unusable transport is skipped, so a hand-edited file can never crash startup.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MCP_CONFIG_PATH = "~/.autobot/mcp/servers.json"

_VALID_TRANSPORTS = {"stdio", "http"}


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One configured MCP server (see ``docs/plans/mcp-integration-design.md`` §5).

    Config only — never secrets. ``secret_ref`` is a Keychain *account name*, not a
    value. ``egress`` is ``"network"`` (sends data off-device, the disclosed
    exception) or ``"local"`` (on-device stdio). ``default_risk`` is the floor for
    this server's tools; ``tool_risk_overrides`` adjusts individual tools.
    """

    id: str
    label: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    auth_type: str = "none"
    token_env: str | None = None
    secret_ref: str | None = None
    enabled: bool = False
    egress: str = "local"
    default_risk: str = "write"
    tool_allow: tuple[str, ...] = ()
    tool_deny: tuple[str, ...] = ()
    tool_risk_overrides: dict[str, str] = field(default_factory=dict)


def _opt_str(value: Any) -> str | None:
    """A non-empty string, or ``None``."""
    return value if isinstance(value, str) and value else None


def _str_tuple(value: Any) -> tuple[str, ...]:
    """A tuple of strings from a JSON list (``()`` if not a list)."""
    return tuple(str(x) for x in value) if isinstance(value, list) else ()


def _str_map(value: Any) -> dict[str, str]:
    """A ``str->str`` map from a JSON object (``{}`` if not an object)."""
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _coerce_server(server_id: str, data: dict[str, Any]) -> McpServerConfig | None:
    """Build one ``McpServerConfig`` from a raw JSON object; ``None`` if unusable."""
    transport = str(data.get("transport", "")).strip()
    if transport not in _VALID_TRANSPORTS:
        return None
    auth = data.get("auth")
    auth_type = str(auth.get("type", "none")) if isinstance(auth, dict) else "none"
    return McpServerConfig(
        id=server_id,
        label=str(data.get("label", server_id)),
        transport=transport,
        command=_opt_str(data.get("command")),
        args=_str_tuple(data.get("args")),
        env=_str_map(data.get("env")),
        url=_opt_str(data.get("url")),
        auth_type=auth_type,
        token_env=_opt_str(data.get("token_env")),
        secret_ref=_opt_str(data.get("secret_ref")),
        enabled=bool(data.get("enabled", False)),
        egress=str(data.get("egress", "local")),
        default_risk=str(data.get("default_risk", "write")),
        tool_allow=_str_tuple(data.get("tool_allow")),
        tool_deny=_str_tuple(data.get("tool_deny")),
        tool_risk_overrides=_str_map(data.get("tool_risk_overrides")),
    )


def _to_json(cfg: McpServerConfig) -> dict[str, Any]:
    """Serialize a config back to the ``servers.json`` descriptor shape (no id key)."""
    return {
        "label": cfg.label,
        "transport": cfg.transport,
        "command": cfg.command,
        "args": list(cfg.args),
        "env": dict(cfg.env),
        "url": cfg.url,
        "auth": {"type": cfg.auth_type},
        "token_env": cfg.token_env,
        "secret_ref": cfg.secret_ref,
        "enabled": cfg.enabled,
        "egress": cfg.egress,
        "default_risk": cfg.default_risk,
        "tool_allow": list(cfg.tool_allow),
        "tool_deny": list(cfg.tool_deny),
        "tool_risk_overrides": dict(cfg.tool_risk_overrides),
    }


def load_mcp_config(path: str | Path = DEFAULT_MCP_CONFIG_PATH) -> dict[str, McpServerConfig]:
    """Load all configured servers, keyed by id. ``{}`` if missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, McpServerConfig] = {}
    for server_id, raw in servers.items():
        if isinstance(raw, dict):
            cfg = _coerce_server(str(server_id), raw)
            if cfg is not None:
                out[str(server_id)] = cfg
    return out


def save_mcp_config(
    servers: dict[str, McpServerConfig], path: str | Path = DEFAULT_MCP_CONFIG_PATH
) -> None:
    """Persist servers to ``servers.json`` (0600), creating parent dirs as needed."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": {sid: _to_json(cfg) for sid, cfg in servers.items()}}
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):  # best effort on exotic filesystems
        p.chmod(0o600)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_config.py -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS (ruff, ruff-format, mypy strict, full pytest suite all green).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/mcp/config.py tests/unit/test_mcp_config.py
git commit -m "feat(mcp): McpServerConfig + servers.json load/save"
```

---

## Self-Review

**1. Spec coverage** (phase-1 line of the design doc: "adapter.py + config.py + ToolSpec.network + registry unregister/replace + Settings.allow_mcp + the mcp extra. Unit tests only; no runtime, no UI"):
- `adapter.py` → Task 5 ✓ (schema map, result→text, risk policy incl. annotations-advisory, fingerprint, namespacing, risk-name parse).
- `config.py` → Task 6 ✓ (descriptor + load/save, matching the §5 JSON shape, including `token_env`).
- `ToolSpec.network` → Task 2 ✓.
- registry `unregister`/replace → Task 3 ✓.
- `Settings.allow_mcp` → Task 4 ✓.
- `mcp` extra → Task 1 ✓.
- "Unit tests only; no runtime, no UI" → no module imports the `mcp` SDK; no daemon/UI touched ✓.
- Design §14 testing list for phase 1 (schema mapping, result-block flattening incl. error/resource, risk policy with overrides + floor, fingerprinting, config load/save + globs/defaults, registry unregister + idempotent re-register) → all covered across Tasks 2,3,5,6 ✓.

**2. Placeholder scan:** no "TBD"/"TODO"/"handle edge cases"/"similar to". Every code step shows complete, runnable code; every run step states the exact command and expected result. ✓

**3. Type consistency:** `risk_for(tool, *, floor, overrides)` signature is identical in the interfaces block, the test calls, and the implementation. `result_to_text -> tuple[str, bool]` consistent. `unregister -> bool` and `register(spec, *, replace=False)` consistent between Task 3's interface, tests, and code. `McpServerConfig` field names used in Task 6 tests (`auth_type`, `token_env`, `tool_allow`, `tool_risk_overrides`, `egress`, `default_risk`) match the dataclass definition exactly. `DEFAULT_MCP_CONFIG_PATH` consistent. ✓

No issues found.
