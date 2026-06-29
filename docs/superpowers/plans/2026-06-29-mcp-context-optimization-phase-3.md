# MCP Context Optimization — Phase 3: Anthropic native Tool Search + prompt caching (cloud path)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Anthropic (cloud) path from advertising every registered tool on every turn. Instead, advertise the always-on **core** tools normally, mark every **gated** tool `defer_loading: true`, and add Anthropic's **server-side Tool Search Tool** (`tool_search_tool_bm25_20251119`) so deferred tools cost ~0 baseline tokens and are loaded on demand by the model. Also put a **prompt-cache breakpoint** on the last tool of the now-stable tool prefix (`cache_control: {"type": "ephemeral"}`) — a lossless cost/latency win. The whole feature is **gated on model/SDK support**: when the configured Claude model can't do tool search (or the capability is switched off), the path degrades to advertising **all** tools — exactly today's behavior — and never crashes startup.

**Architecture:** One seam, on the cloud path only. `AnthropicLanguageModel.run_turn` stops calling `to_anthropic_tools(self._registry.schemas())` (which flattens *every* tool, no tiering, no caching) and instead calls a new local helper, `self._assemble_tools()`, that:
1. partitions `registry.specs()` into **core** (advertised normally) vs **gated** (marked `defer_loading: true`), reusing the same `ToolSpec.core` tiering Phase 1 added — but the partition logic is implemented **locally in `anthropic_llm.py`** (we do NOT import `tools/selection.py`; see the cross-phase isolation note);
2. appends the Tool Search Tool (`{"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}`), which must **not** be deferred and guarantees the required ≥1 non-deferred tool;
3. stamps `cache_control: {"type": "ephemeral"}` on the **last** tool so the whole (now-static) tool prefix is cached;
4. is short-circuited by a capability flag — when tool search is unsupported/disabled, it falls back to the legacy `to_anthropic_tools(self._registry.schemas())` (all tools, no `defer_loading`, no tool-search tool), so the cloud path is byte-for-byte the pre-Phase-3 request.

Capability is decided once in `__init__` (try/except + log + degrade) and recorded on `self._tool_search`. No orchestrator change, no protocol change, no change to the Ollama path.

**Tech Stack:** Python 3.11, the already-declared `anthropic` extra (SDK **0.109.2**, which exposes `ToolSearchToolBm25_20251119Param`, `defer_loading` on `ToolParam`, and tool `cache_control` on the standard `messages.create` `tools` union — no beta namespace/header needed), pytest, mypy strict, ruff. **No new runtime dependency.**

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- mypy **strict** over `src` AND `tests` — keep it green. Run `uv run mypy`. The `anthropic` SDK is already in mypy's ignore list (`pyproject.toml` `[[tool.mypy.overrides]]` → `"anthropic.*"`), so keep its imports **lazy** (inside `__init__`/methods) exactly as `anthropic_llm.py` already does — the module-level code must stay import-light and dependency-free.
- Google-style docstrings on every public module/class/function (ruff `D`); **tests exempt**.
- Line length 100; never hand-format — run `uv run ruff format .` (or `make format`).
- Value objects are `frozen=True, slots=True` dataclasses; no business logic on them.
- **No new runtime dependency** — Tool Search and caching are SDK features already shipped in the declared `anthropic` extra; we send plain dicts, no new import.
- **Cloud features degrade gracefully — never crash startup.** Mirror the existing `_build_llm` pattern (try/except + `_log` + `print` + fall back to a working path). **Never log tokens or secrets** (the existing `_log_usage` logs counts only; keep it that way — do not add anything that prints schemas/keys).
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- Verification gate per task: `make check` green (ruff + ruff-format + mypy + pytest). Run a single file with `uv run pytest tests/unit/<file>.py -v`.
- **Branch:** continue on `feat/mcp-integration`. All Phase-3 commits stack there.

**Interfaces already on the branch (consume these — do NOT rebuild):**
- `autobot.tools.registry.ToolSpec` — `frozen=True, slots=True` dataclass with `name, description, parameters, handler, risk, confirm_prompt, ack, requires, network, core: bool = False`; `to_schema() -> dict`. (Phase 1 added `core`; Phase 1's Task 6 marked the ~14 frequent built-ins `core=True`. MCP tools are never `core`.)
- `autobot.tools.registry.ToolRegistry` — `register(...)`, `unregister(...)`, `get(...)`, `schemas() -> list[dict]`, `specs() -> list[ToolSpec]` (Phase 1), `dispatch(...)`.
- `autobot.config.Settings` — `frozen=True, slots=True`; existing cloud fields `llm_provider`, `anthropic_model` (default `"claude-haiku-4-5"`), `anthropic_max_tokens`, `anthropic_context_tokens`; existing tiering fields `tool_budget`, `tool_selection` (`"lexical"`/`"all"`/future `"embedding"`), `tool_core_extra`, `tool_core_remove`. `Settings.load(path)` overlays JSON via `_coerce`; `config.py` already imports `field` from `dataclasses`.
- `autobot.llm.anthropic_llm.AnthropicLanguageModel(settings, registry, transcript=None, memory=None, api_key=None, client=None)` — `run_turn` builds `tools = to_anthropic_tools(self._registry.schemas())` (≈L540), then `_send(tools, overhead)` calls `self._client.messages.create(..., tools=tools)` (≈L505-512). `to_anthropic_tools(schemas)` maps OpenAI-style → Anthropic `input_schema` shape. `with_cache_breakpoint(messages)` already caches the message prefix (a *message-block* breakpoint — orthogonal to the new *tool* breakpoint).
- `autobot.app._build_llm(settings, registry, transcript, memory)` — the `anthropic` branch (≈L277-296) builds `AnthropicLanguageModel(settings, registry, transcript, memory=memory)`; the Ollama branch (≈L297-300) builds the selector. Phase 3 leaves the Ollama branch untouched.
- Test helpers in `tests/unit/test_anthropic_llm.py`: `FakeMessages` (records `self.calls` = list of `create(**kwargs)` dicts, so `calls[i]["tools"]` is the advertised tool list), `FakeClient(responses)`, `_block(**kw)`, `_registry()`, `Settings(llm_provider="anthropic")`.

## Cross-phase isolation (MUST honor)

Phase 3 touches **only**: `src/autobot/llm/anthropic_llm.py`, the **anthropic branch** of `src/autobot/app.py::_build_llm`, `src/autobot/config.py` (one new flag), and their tests. It does **NOT** modify `src/autobot/core/interfaces.py`, `src/autobot/tools/selection.py`, or `src/autobot/tools/registry.py` (Phase 2 owns the `find_tools` escape hatch in the turn loop; Phase 4 owns `selection.py`/`EmbeddingToolSelector`). The core-vs-gated partition needed here is implemented **locally** in `anthropic_llm.py` from `registry.specs()` + `spec.core` (a 3-line helper), **not** by importing `LexicalToolSelector`/`build_tool_selector`. The cloud path uses Anthropic's *native* server-side search, not the client-side lexical selector — the lexical selector remains the local/Ollama path's tool. (The new `anthropic_tool_search` flag's `"lexical"` value is the one place a future change *could* wire the client-side pre-filter as a fallback; this plan implements `"auto"`/`"on"`/`"off"` only, and `"off"` degrades to advertise-all.)

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/config.py` (modify) | Add `anthropic_tool_search: str = "auto"` (`"auto"`/`"on"`/`"off"`) |
| `src/autobot/llm/anthropic_llm.py` (modify) | Add module constants + pure helpers `tool_search_supported`, `partition_tools`, `assemble_anthropic_tools`; decide capability in `__init__` (`self._tool_search`); call `self._assemble_tools()` from `run_turn` instead of `to_anthropic_tools(self._registry.schemas())` |
| `src/autobot/app.py` (modify) | No behavior change to the anthropic branch (it already passes `settings`); add a one-line `_log.info` of the resolved tool-search mode if absent — see Task 5 (no-op safe). The anthropic branch stays the fallback-safe try/except it is. |
| `tests/unit/test_config.py` (modify) | `anthropic_tool_search` default + overlay |
| `tests/unit/test_anthropic_llm.py` (modify) | Pure-helper tests + fake-client tests: gated carry `defer_loading`, core don't, search tool present & not deferred, `cache_control` on last tool, fallback advertises all |

---

### Task 1: `anthropic_tool_search` setting

**Files:**
- Modify: `src/autobot/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.anthropic_tool_search: str = "auto"` — `"auto"` (use native Tool Search when the configured model supports it, else advertise-all), `"on"` (force native Tool Search regardless of the support table — for testing a new model), `"off"` (never use it — always advertise all tools, today's behavior).

**Context:** `Settings` is `frozen=True, slots=True`; `config.py` already imports `field` (Phase 1). Add the new field inside the existing `# --- tool selection / context budget ---` block so all tiering tunables sit together. It's a cloud-path knob with no secret.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py` (it already imports `Settings`/`write_settings`/`Path` from Phase 1):

```python
def test_anthropic_tool_search_defaults_auto() -> None:
    assert Settings().anthropic_tool_search == "auto"


def test_anthropic_tool_search_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"anthropic_tool_search": "off"}, path)
    assert Settings.load(path).anthropic_tool_search == "off"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k anthropic_tool_search -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'anthropic_tool_search'`.

- [ ] **Step 3: Add the setting**

In `src/autobot/config.py`, inside the `# --- tool selection / context budget ---` block, immediately after the `tool_core_remove: list[str] = field(default_factory=list)` line (just before `# --- daemon (Phase 3c) ---`), add:

```python
    # Anthropic-only: how the cloud path advertises tools. "auto" (default) uses
    # Anthropic's server-side Tool Search Tool when the configured model supports it
    # (gated built-ins marked defer_loading so connecting MCP servers add ~0 baseline
    # tokens; the search tool loads them on demand) and otherwise advertises every
    # tool. "on" forces native Tool Search regardless of the support table (to try a
    # newer model). "off" always advertises every tool — the pre-optimization cloud
    # behavior, for debugging/comparison. The local (Ollama) path is unaffected; it
    # uses tool_selection instead.
    anthropic_tool_search: str = "auto"
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_config.py -v` → PASS (all, incl. the 2 new).
Run: `uv run mypy` → `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/config.py tests/unit/test_config.py
git commit -m "feat(config): anthropic_tool_search setting (auto/on/off) for native cloud tool search"
```

---

### Task 2: Pure helpers — capability gate + tiered tool assembly

**Files:**
- Modify: `src/autobot/llm/anthropic_llm.py`
- Test: `tests/unit/test_anthropic_llm.py`

**Interfaces:**
- Consumes: `ToolSpec`, `to_anthropic_tools` (existing).
- Produces (all pure, module-level, unit-testable with no client/network):
  - Constants `TOOL_SEARCH_TYPE = "tool_search_tool_bm25_20251119"`, `TOOL_SEARCH_NAME = "tool_search_tool_bm25"`, and `_TOOL_SEARCH_MODEL_PREFIXES: tuple[str, ...]` (the models known to support server-side tool search).
  - `tool_search_supported(model: str, mode: str) -> bool` — resolves the `anthropic_tool_search` mode against the model: `"off"` → `False`; `"on"` → `True`; `"auto"` → prefix-match against `_TOOL_SEARCH_MODEL_PREFIXES`.
  - `partition_tools(specs: Sequence[ToolSpec]) -> tuple[list[ToolSpec], list[ToolSpec]]` — `(core, gated)` by `spec.core` (the only place tiering is read on this path; no `selection.py` import).
  - `assemble_anthropic_tools(specs, *, tool_search: bool) -> list[dict[str, Any]]` — when `tool_search` is `False`, returns `to_anthropic_tools([s.to_schema() for s in specs])` (legacy: every tool, no `defer_loading`, no search tool). When `True`: core tools advertised normally, gated tools each get `"defer_loading": True`, the Tool Search Tool is appended (not deferred), and `cache_control: {"type": "ephemeral"}` is stamped on the **last** tool of the list.

**Context (verified against installed SDK 0.109.2):** `ToolUnionParam` accepts `ToolSearchToolBm25_20251119Param` (`name="tool_search_tool_bm25"`, `type="tool_search_tool_bm25_20251119"`) and `ToolParam` carries `defer_loading: bool` and `cache_control` — all on the **standard** `messages.create(tools=...)` (no beta header). Anthropic requires **≥1 non-deferred tool**, and the **search tool itself must NOT be deferred** — appending the (non-deferred) search tool satisfies both even if every `ToolSpec` is gated. We send plain dicts (matching how `to_anthropic_tools` already builds tool dicts), so nothing new is imported and mypy's `anthropic.*` ignore is irrelevant here.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_anthropic_llm.py`. Add these names to the existing top-of-file import from `autobot.llm.anthropic_llm`:

```python
from autobot.llm.anthropic_llm import (
    TOOL_SEARCH_NAME,
    TOOL_SEARCH_TYPE,
    assemble_anthropic_tools,
    partition_tools,
    tool_search_supported,
)
```

Then append these tests (helper to build specs, then the assertions):

```python
def _spec(name: str, *, core: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"desc for {name}",
        parameters={"type": "object", "properties": {}},
        handler=lambda **k: name,
        core=core,
    )


def test_tool_search_supported_resolves_mode_and_model() -> None:
    # "off" always disables; "on" always enables; "auto" follows the model table.
    assert tool_search_supported("claude-opus-4-8", "off") is False
    assert tool_search_supported("some-unknown-model", "on") is True
    assert tool_search_supported("claude-opus-4-8", "auto") is True
    assert tool_search_supported("claude-haiku-4-5", "auto") is False  # not in the table


def test_partition_tools_splits_core_from_gated() -> None:
    core, gated = partition_tools([_spec("battery", core=True), _spec("slack__send")])
    assert [s.name for s in core] == ["battery"]
    assert [s.name for s in gated] == ["slack__send"]


def test_assemble_marks_gated_defer_and_keeps_core_undeferred() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=True
    )
    by_name = {t["name"]: t for t in tools}
    assert "defer_loading" not in by_name["battery"]  # core advertised normally
    assert by_name["slack__send"]["defer_loading"] is True  # gated → deferred


def test_assemble_adds_search_tool_not_deferred() -> None:
    tools = assemble_anthropic_tools([_spec("slack__send")], tool_search=True)
    search = next(t for t in tools if t.get("type") == TOOL_SEARCH_TYPE)
    assert search["name"] == TOOL_SEARCH_NAME
    assert "defer_loading" not in search  # the search tool must never be deferred
    # At least one non-deferred tool always exists (the search tool), as required.
    assert any("defer_loading" not in t for t in tools)


def test_assemble_puts_cache_control_on_last_tool_only() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=True
    )
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in tools[:-1])


def test_assemble_fallback_advertises_all_without_defer_or_search() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=False
    )
    names = {t["name"] for t in tools}
    assert names == {"battery", "slack__send"}  # every tool, none dropped
    assert all("defer_loading" not in t for t in tools)  # legacy: no deferral
    assert all(t.get("type") != TOOL_SEARCH_TYPE for t in tools)  # no search tool
    assert all("cache_control" not in t for t in tools)  # legacy request unchanged
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "tool_search_supported or partition_tools or assemble" -v`
Expected: FAIL — `ImportError: cannot import name 'assemble_anthropic_tools' from 'autobot.llm.anthropic_llm'`.

- [ ] **Step 3: Add the constants**

In `src/autobot/llm/anthropic_llm.py`, immediately after the `_MAX_TOKENS_RE = re.compile(...)` line (≈L60, the end of the existing module-constants block), add:

```python
# Anthropic server-side Tool Search Tool (verified against SDK 0.109.2 —
# ToolSearchToolBm25_20251119Param is in the standard messages.create tools union,
# so no beta header is needed). The model discovers deferred tools by searching this
# tool; deferred tool defs cost ~0 baseline tokens until loaded on demand.
TOOL_SEARCH_TYPE = "tool_search_tool_bm25_20251119"
TOOL_SEARCH_NAME = "tool_search_tool_bm25"
# Claude models known to support server-side tool search (prefix match, like
# _MODEL_WINDOWS). "auto" mode enables tool search only for these; an unlisted model
# (e.g. the default claude-haiku-4-5) safely advertises all tools instead. Extend
# this as Anthropic adds support; "on" in settings forces it for a model not yet here.
_TOOL_SEARCH_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)
```

- [ ] **Step 4: Add the pure helpers**

In `src/autobot/llm/anthropic_llm.py`, immediately after `to_anthropic_tools(...)` (it ends at ≈L130), add:

```python
def tool_search_supported(model: str, mode: str) -> bool:
    """Whether to use Anthropic's native Tool Search Tool for ``model``.

    Resolves the ``anthropic_tool_search`` setting: ``"off"`` never uses it,
    ``"on"`` always does (to try a model not yet in the table), and ``"auto"``
    enables it only for models known to support it (prefix match against
    :data:`_TOOL_SEARCH_MODEL_PREFIXES`). Unknown ``mode`` is treated as ``"auto"``.
    """
    if mode == "off":
        return False
    if mode == "on":
        return True
    return any(model.startswith(p) for p in _TOOL_SEARCH_MODEL_PREFIXES)


def partition_tools(specs: Sequence[ToolSpec]) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """Split specs into ``(core, gated)`` by :attr:`ToolSpec.core`.

    Core tools are advertised to the cloud model normally; gated tools are deferred
    (``defer_loading``) and discovered via tool search. This is the only place the
    cloud path reads the tiering flag — it deliberately does not import the local
    ``tools.selection`` module (the cloud path uses native server-side search, not the
    client-side lexical selector).
    """
    core = [s for s in specs if s.core]
    gated = [s for s in specs if not s.core]
    return core, gated


def assemble_anthropic_tools(
    specs: Sequence[ToolSpec], *, tool_search: bool
) -> list[dict[str, Any]]:
    """Build the cloud ``tools`` payload: tiered + search + cached, or legacy all-tools.

    When ``tool_search`` is ``False`` (model/SDK unsupported, or disabled in settings),
    returns every tool exactly as before — the pre-Phase-3 behavior — so the request is
    byte-for-byte the legacy one. When ``True``: core tools are advertised normally,
    gated tools each get ``defer_loading: True`` (so connecting MCP servers add ~0
    baseline tokens), the non-deferred Tool Search Tool is appended (it both lets the
    model load deferred tools and guarantees the required ≥1 non-deferred tool), and a
    ``cache_control`` ephemeral breakpoint is stamped on the last tool so the whole
    now-static tool prefix is prompt-cached.
    """
    if not tool_search:
        return to_anthropic_tools([s.to_schema() for s in specs])
    core, gated = partition_tools(specs)
    tools: list[dict[str, Any]] = []
    for s in core:
        tools.append(
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.parameters or {"type": "object", "properties": {}},
            }
        )
    for s in gated:
        tools.append(
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.parameters or {"type": "object", "properties": {}},
                "defer_loading": True,
            }
        )
    tools.append({"type": TOOL_SEARCH_TYPE, "name": TOOL_SEARCH_NAME})
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools
```

`Sequence` and `ToolSpec` must be importable. The module already imports `from typing import TYPE_CHECKING, Any` and `from autobot.tools.registry import ToolRegistry` at runtime. Add `ToolSpec` to that runtime import and add `Sequence`:

In `src/autobot/llm/anthropic_llm.py`, change:
```python
from typing import TYPE_CHECKING, Any
```
to:
```python
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
```
and change:
```python
from autobot.tools.registry import ToolRegistry
```
to:
```python
from autobot.tools.registry import ToolRegistry, ToolSpec
```

(`ToolSpec` is a pure dataclass — importing it adds no heavy runtime; this matches the existing eager `ToolRegistry` import.)

- [ ] **Step 5: Run tests + mypy**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "tool_search_supported or partition_tools or assemble" -v` → PASS (6).
Run: `uv run mypy` → `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/llm/anthropic_llm.py tests/unit/test_anthropic_llm.py
git commit -m "feat(llm): pure tiered Anthropic tool assembly (defer_loading + tool search + cache)"
```

---

### Task 3: Decide capability in `__init__` (gated, graceful)

**Files:**
- Modify: `src/autobot/llm/anthropic_llm.py`
- Test: `tests/unit/test_anthropic_llm.py`

**Interfaces:**
- Consumes: `tool_search_supported`, `Settings.anthropic_tool_search`, `Settings.anthropic_model`.
- Produces: `self._tool_search: bool` set once in `__init__` (after `self._window` is resolved), logged at INFO. Never raises — a bad/unknown `anthropic_tool_search` value resolves via `tool_search_supported` (unknown mode → `"auto"`), so startup can't crash on this flag.

**Context:** `__init__` (≈L269) ends by resolving `self._window` and logging `"cloud LLM ready ..."` (≈L315-318). We decide tool-search capability right there, alongside the window, and fold it into that one log line (no tokens/secrets — just the model name and a bool). This is the "gate on support" decision point; `run_turn`/`_send` then just read the bool.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_anthropic_llm.py`:

```python
def test_tool_search_capability_on_for_supported_model() -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="claude-opus-4-8"),
        _registry(),
        client=FakeClient([]),
    )
    assert model._tool_search is True


def test_tool_search_capability_off_for_default_model_in_auto() -> None:
    # Default model (claude-haiku-4-5) is not in the support table → auto disables it.
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    assert model._tool_search is False


def test_tool_search_capability_forced_on_by_setting() -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_tool_search="on"),
        _registry(),
        client=FakeClient([]),
    )
    assert model._tool_search is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "tool_search_capability" -v`
Expected: FAIL — `AttributeError: 'AnthropicLanguageModel' object has no attribute '_tool_search'`.

- [ ] **Step 3: Decide + store the capability**

In `src/autobot/llm/anthropic_llm.py`, in `__init__`, replace the closing block:

```python
        self._window = self._resolve_window()
        _log.info(
            "cloud LLM ready model=%s context_window=%d", settings.anthropic_model, self._window
        )
```

with:

```python
        self._window = self._resolve_window()
        # Gate on model support (auto), or honor an explicit on/off. Pure decision —
        # never raises — so a misconfigured flag degrades to advertise-all, never a
        # startup crash. Logged as a bool only (no tokens/secrets).
        self._tool_search = tool_search_supported(
            settings.anthropic_model, settings.anthropic_tool_search
        )
        _log.info(
            "cloud LLM ready model=%s context_window=%d tool_search=%s",
            settings.anthropic_model,
            self._window,
            self._tool_search,
        )
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "tool_search_capability" -v` → PASS (3).
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/llm/anthropic_llm.py tests/unit/test_anthropic_llm.py
git commit -m "feat(llm): resolve Anthropic tool-search capability once at init (gated, graceful)"
```

---

### Task 4: Wire `_assemble_tools()` into `run_turn`

**Files:**
- Modify: `src/autobot/llm/anthropic_llm.py`
- Test: `tests/unit/test_anthropic_llm.py`

**Interfaces:**
- Consumes: `assemble_anthropic_tools`, `self._tool_search`, `self._registry.specs()`.
- Produces: `AnthropicLanguageModel._assemble_tools() -> list[dict[str, Any]]` (reads the live registry each turn, so MCP tools that connect/disconnect are picked up). `run_turn` builds tools via it instead of `to_anthropic_tools(self._registry.schemas())`. The `overhead` estimate and `_send`/`messages.create(..., tools=tools)` flow are unchanged — `tools` is just assembled differently.

**Context:** `run_turn` builds `tools = to_anthropic_tools(self._registry.schemas())` at ≈L540 and immediately estimates `overhead` from `len(str(t)) for t in tools` (≈L541). `_send(tools, overhead)` passes `tools=tools` straight to `messages.create` (≈L505-512), which is the standard endpoint that accepts the tool-search tool + `defer_loading` + tool `cache_control` in SDK 0.109.2. Only the one assembly line changes; the rest of the loop, caching of *messages* (`with_cache_breakpoint`), trimming, and usage logging are untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_anthropic_llm.py`:

```python
def _tiered_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="battery_status",
            description="Check the Mac's battery level.",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "100%",
            core=True,
        )
    )
    reg.register(
        ToolSpec(
            name="slack__send",
            description="Send a Slack message.",
            parameters={"type": "object", "properties": {}},
            handler=lambda **k: "sent",
        )
    )
    return reg


def test_run_turn_advertises_tiered_tools_when_search_supported() -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="claude-opus-4-8"),
        _tiered_registry(),
        client=FakeClient([resp]),
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    sent = model._client.messages.calls[0]["tools"]
    by_name = {t.get("name"): t for t in sent}
    assert "defer_loading" not in by_name["battery_status"]   # core advertised normally
    assert by_name["slack__send"]["defer_loading"] is True    # gated → deferred
    assert TOOL_SEARCH_NAME in by_name                        # search tool present
    assert "defer_loading" not in by_name[TOOL_SEARCH_NAME]   # and never deferred
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}  # cache on the last tool


def test_run_turn_advertises_all_tools_when_search_unsupported() -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"),  # default model → auto disables search
        _tiered_registry(),
        client=FakeClient([resp]),
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    sent = model._client.messages.calls[0]["tools"]
    names = {t.get("name") for t in sent}
    assert names == {"battery_status", "slack__send"}        # every tool, legacy shape
    assert TOOL_SEARCH_NAME not in names                     # no search tool
    assert all("defer_loading" not in t for t in sent)       # no deferral
    assert all("cache_control" not in t for t in sent)       # legacy request unchanged
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "advertises_tiered or advertises_all" -v`
Expected: FAIL — the tools still come from `to_anthropic_tools(self._registry.schemas())`, so `slack__send` has no `defer_loading` and there is no search tool / no tool `cache_control`.

- [ ] **Step 3: Add `_assemble_tools` + use it in `run_turn`**

In `src/autobot/llm/anthropic_llm.py`, add this method to `AnthropicLanguageModel` immediately before `run_turn` (≈L530):

```python
    def _assemble_tools(self) -> list[dict[str, Any]]:
        """Tools to advertise this turn: tiered + tool-search + cached, or all tools.

        Reads the live registry, so MCP tools that connect/disconnect between turns
        are reflected. When tool search is supported (see :meth:`__init__`), gated
        tools are deferred and the search tool loads them on demand; otherwise every
        tool is advertised — the pre-Phase-3 behavior — so the path degrades cleanly.
        """
        return assemble_anthropic_tools(self._registry.specs(), tool_search=self._tool_search)
```

Then, in `run_turn`, replace:

```python
        tools = to_anthropic_tools(self._registry.schemas())
```

with:

```python
        tools = self._assemble_tools()
```

(The next line — `overhead = (len(self._system()) + sum(len(str(t)) for t in tools)) // _CHARS_PER_TOKEN` — already adapts to the smaller deferred payload, so deferred tools also shrink the trim overhead estimate. No other change.)

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -k "advertises_tiered or advertises_all" -v` → PASS (2).
Run: `uv run pytest tests/unit/test_anthropic_llm.py -v` → PASS (all — the pre-existing loop/caching/window/usage tests still pass: they use `_registry()` with the default model `claude-haiku-4-5`, so `self._tool_search` is `False` and the advertised tools are the legacy all-tools shape the existing assertions expect).
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/llm/anthropic_llm.py tests/unit/test_anthropic_llm.py
git commit -m "feat(llm): advertise tiered/deferred tools on the Anthropic path with safe fallback"
```

---

### Task 5: Log the resolved cloud tool-search mode in `_build_llm` (observability + final gate)

**Files:**
- Modify: `src/autobot/app.py`
- Test: `tests/unit/test_app_selector.py` (reuse the Phase-1 file)

**Interfaces:** Consumes the `anthropic` branch of `_build_llm` (unchanged construction). Produces: the anthropic branch still builds `AnthropicLanguageModel(settings, registry, transcript, memory=memory)` and degrades to local on `ImportError`/`ValueError` exactly as today; we only enrich the disclosure `print`/log so an operator can see the tool-search mode at a glance. The capability decision itself lives in the model's `__init__` (Task 3) — this is purely the composition-root seam log, mirroring how Phase 1 logged the local selector.

**Context:** `_build_llm`'s anthropic branch (≈L277-296) logs `"llm provider=anthropic model=%s (OFF-DEVICE)"` and prints the disclosure banner. We add the resolved tool-search *mode setting* (not a token/secret) to the existing INFO log so the daemon log shows it. The model's own `__init__` already logs the resolved bool; this adds the *configured intent* at the composition root. This task also carries the full-suite gate.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_app_selector.py` (it already imports `_build_llm`, `Settings`, `ToolRegistry`, `NullTranscript`):

```python
def test_build_llm_anthropic_off_falls_back_to_local_without_key() -> None:
    # No API key + default provider switch to anthropic: _build_llm must NOT raise; it
    # degrades to the local Ollama model (cloud features never crash startup).
    from autobot.llm.ollama_llm import OllamaLanguageModel

    model = _build_llm(
        Settings(context_tokens=4096, llm_provider="anthropic"),
        ToolRegistry(),
        NullTranscript(),
        None,
    )
    assert isinstance(model, OllamaLanguageModel)  # fell back to local, no crash
```

(This test relies only on there being no Anthropic key in the test environment's Keychain — `AnthropicLanguageModel.__init__` raises `ValueError` via `_require_key`, which the branch catches and falls back. If a key *is* present in the dev Keychain, the branch instead returns the cloud model with an injected real client — to keep the test hermetic, it asserts the fallback only; should a key be present locally, skip with `pytest.importorskip`-style guard is unnecessary because CI has no key. Document this in the smoke-test section.)

- [ ] **Step 2: Run to verify the new log line is absent / test passes pre-change**

Run: `uv run pytest tests/unit/test_app_selector.py -v`
Expected: the new fallback test PASSES already (the branch's graceful degrade predates Phase 3) — it is a **regression guard** proving Phase 3's added log line doesn't break the fallback. The log-enrichment in Step 3 is behavior-preserving; this test pins that.

- [ ] **Step 3: Enrich the disclosure log (no behavior change)**

In `src/autobot/app.py`, in the `anthropic` branch of `_build_llm`, change:

```python
            log.info("llm provider=anthropic model=%s (OFF-DEVICE)", settings.anthropic_model)
```

to:

```python
            log.info(
                "llm provider=anthropic model=%s tool_search=%s (OFF-DEVICE)",
                settings.anthropic_model,
                settings.anthropic_tool_search,
            )
```

(No new import, no control-flow change — the `try`/`except ImportError`/`except ValueError` fallback to local stays exactly as is.)

- [ ] **Step 4: Run tests + import smoke + mypy**

Run: `uv run pytest tests/unit/test_app_selector.py -v` → PASS.
Run: `uv run python -c "from autobot.app import build; print('build-import-ok')"` → prints `build-import-ok`.
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS (ruff + ruff-format + mypy strict + full pytest suite all green).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/app.py tests/unit/test_app_selector.py
git commit -m "feat(app): log resolved Anthropic tool-search mode + guard graceful cloud fallback"
```

---

## Manual smoke-test (after Task 5)

The live cloud flow can't run in CI (no API key). Verify it manually with a real Anthropic key in the Keychain (`autobot.secrets` → `anthropic_api_key`):

1. **Supported model, tiering on.** Set `~/.autobot/settings.json` to `{"llm_provider": "anthropic", "anthropic_model": "claude-opus-4-8"}` (leave `anthropic_tool_search` at its `"auto"` default). `make run`. The daemon log (`make logs-grep C=llm`) should show `tool_search=True`. Ask **"hi"** — confirm a normal reply, and that the request advertised only the ~14 core tools + the search tool (`grep '\[llm\]' ~/.autobot/logs/autobot.log` — the context line should be far smaller than pre-Phase-3, and `cache_read` should climb after turn 1, proving the tool prefix is cached).
2. **MCP gated, ~0 baseline.** With `allow_mcp` + an MCP server (e.g. Slack) enabled, ask **"hi"** again — confirm the MCP tools are *not* in the baseline request (they're deferred). Then ask something in that server's domain (e.g. **"send a Slack message to #general saying hello"**) — confirm the model calls `tool_search_tool_bm25`, the deferred Slack tool loads server-side, and the action then runs **through the local permission gate** (a grant card appears for the network-egress write — the gate is untouched by Phase 3).
3. **Unsupported model → fallback.** Set `"anthropic_model": "claude-haiku-4-5"` (the default) and restart. Log shows `tool_search=False`. Confirm the path still works and advertises **all** tools (the legacy shape) — i.e. graceful degrade, no crash, no `defer_loading`.
4. **Force on / off.** Set `"anthropic_tool_search": "on"` with the haiku model to try server-side search on an unlisted model (confirm it either works or the API error is caught and the calm reply is spoken — never a crash). Set `"anthropic_tool_search": "off"` to force advertise-all even on opus (comparison/debugging). Restart between changes.
5. **No tokens/secrets leaked.** Grep the log for the key and confirm it never appears: `grep -i "sk-ant\|api_key" ~/.autobot/logs/autobot.log` returns nothing.

---

## Self-Review

**1. Spec coverage** (design §6 "Anthropic cloud path": advertise core normally; mark gated `defer_loading: true`; add the tool-search tool; enable prompt caching on the stable tool prefix; gated on model support with graceful fallback to lexical/all):
- Core advertised normally; gated `defer_loading: true`; tool-search tool appended (not deferred); `cache_control` on the last (stable-prefix) tool → `assemble_anthropic_tools` (Task 2), exercised end-to-end in `run_turn` (Task 4) ✓.
- "At least one non-deferred tool required; the search tool itself must NOT be deferred" → the non-deferred search tool is always appended, so even an all-gated registry satisfies both; asserted in `test_assemble_adds_search_tool_not_deferred` (Task 2) ✓.
- Tool-search tool type/name match the **installed SDK 0.109.2** exactly (`tool_search_tool_bm25_20251119` / `tool_search_tool_bm25`, on the standard `messages.create` tools union — verified, no beta header) → constants in Task 2 ✓.
- Prompt caching is the lossless cost/latency bonus on the now-stable tool prefix; the existing message-level `with_cache_breakpoint` is left intact (orthogonal) → Task 2/Task 4 ✓.
- **Gated on model/SDK support with graceful fallback, never crashes startup** → capability resolved once in `__init__` via pure `tool_search_supported` (Task 3); `assemble_anthropic_tools(..., tool_search=False)` reproduces the exact legacy all-tools request (Task 2/Task 4); `_build_llm`'s existing try/except still degrades to local on missing extra/key, regression-guarded (Task 5) ✓.
- Config flag introduced and documented: `anthropic_tool_search` (`"auto"`/`"on"`/`"off"`), default `"auto"`; `"off"` = advertise-all = pre-optimization behavior → Task 1 ✓.
- **Fully testable offline** with `FakeClient`: gated carry `defer_loading`, core don't, search tool present & not deferred, `cache_control` on the last tool, fallback advertises all → Tasks 2 & 4 assert every one; live flow is the manual smoke-test (no key in CI), documented above ✓.

**2. Cross-phase isolation honored:** Phase 3 modifies only `config.py` (one flag), `anthropic_llm.py`, the anthropic branch of `app._build_llm` (log-only), and their tests. It does **not** touch `core/interfaces.py`, `tools/selection.py`, or `tools/registry.py`. The core/gated partition is a local 3-line helper (`partition_tools`) reading `registry.specs()` + `spec.core` — no `LexicalToolSelector`/`build_tool_selector` import. Stated explicitly in the "Cross-phase isolation" section ✓.

**3. Placeholder scan:** none — every code step shows complete, runnable code grounded in the real current `anthropic_llm.py` (the `to_anthropic_tools(self._registry.schemas())` line at ≈L540, the `__init__` window/log block at ≈L315-318, `_send` passing `tools=tools` to `messages.create`); every run step states the command and expected result. The tool-search type/name, `defer_loading`, and tool `cache_control` are all verified present in the installed SDK 0.109.2.

**4. Type consistency:**
- `tool_search_supported(model: str, mode: str) -> bool` — defined Task 2, called in `__init__` (Task 3); the result stored as `self._tool_search: bool`.
- `partition_tools(specs: Sequence[ToolSpec]) -> tuple[list[ToolSpec], list[ToolSpec]]` — defined Task 2, used only inside `assemble_anthropic_tools` (Task 2).
- `assemble_anthropic_tools(specs: Sequence[ToolSpec], *, tool_search: bool) -> list[dict[str, Any]]` — defined Task 2, called by `_assemble_tools` (Task 4) with `self._registry.specs()` (`list[ToolSpec]`, a `Sequence[ToolSpec]`) and `tool_search=self._tool_search` (`bool`).
- `AnthropicLanguageModel._assemble_tools(self) -> list[dict[str, Any]]` — defined Task 4; its result feeds `_send(tools, overhead)` whose `tools: list[dict[str, Any]]` parameter and `messages.create(tools=tools)` are unchanged.
- `Settings.anthropic_tool_search: str` — defined Task 1, read in `__init__` (Task 3) and `_build_llm` (Task 5).
- `ToolSpec.core: bool` / `ToolRegistry.specs() -> list[ToolSpec]` — Phase-1 interfaces, consumed read-only here (not modified).

No issues found.
