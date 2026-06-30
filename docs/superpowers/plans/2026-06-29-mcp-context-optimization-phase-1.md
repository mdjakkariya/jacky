# MCP Context Optimization — Phase 1: Relevance-Gated Tool Advertising (local/Ollama)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop advertising every registered tool on every turn. Introduce a swappable `ToolSelector` that returns a bounded, relevance-ranked subset (always-on **core** built-ins + top-K **gated** tools by lexical match to the user's message), wired into the Ollama client. MCP tools stay gated, so connecting servers adds ~0 baseline tokens.

**Architecture:** One seam. The Ollama client stops calling `registry.schemas()` blindly and instead asks an injected `ToolSelector` for the round's tools. `ToolSpec` gains a `core: bool` flag; the registry gains `specs()`. A pure lexical scorer (BM25-style term overlap with IDF weighting, zero deps) ranks gated tools. Selection logic lives in one pure, unit-tested module (`tools/selection.py`); the `ToolSelector` protocol lives in `core/interfaces.py`. No orchestrator change. Behavior is backward-compatible: a client built without a selector advertises all tools exactly as today.

**Tech Stack:** Python 3.11, dataclasses, `re`/`math` (stdlib only — no new deps), pytest, mypy strict, ruff.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- mypy **strict** over `src` AND `tests` — keep it green. Run `uv run mypy`.
- Google-style docstrings on every public module/class/function (ruff `D`); **tests exempt**.
- Line length 100; never hand-format — run `uv run ruff format .` (or `make format`).
- Value objects are `frozen=True, slots=True` dataclasses; no business logic on them.
- **No new runtime dependency** — lexical selection is stdlib-only and 100% on-device.
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- Verification gate per task: `make check` green (ruff + ruff-format + mypy + pytest). Run a single file with `uv run pytest tests/unit/<file>.py -v`.
- **Branch:** continue on `feat/mcp-integration`. All Phase-1 commits stack there.

**Interfaces already on the branch (consume these):**
- `autobot.tools.registry.ToolSpec` — `frozen=True, slots=True` dataclass: `name, description, parameters, handler, risk=Risk.READ_ONLY, confirm_prompt=None, ack=None, requires=None, network=False`; `to_schema() -> dict`.
- `autobot.tools.registry.ToolRegistry` — `register(spec, *, replace=False)`, `unregister(name)->bool`, `get(name)->ToolSpec|None`, `schemas()->list[dict]`, `dispatch(...)`. Holds `self._tools: dict[str,ToolSpec]` + `self._lock`.
- `autobot.config.Settings` — `frozen=True, slots=True`; `Settings.load(path)` overlays JSON via `_coerce`.
- `autobot.llm.ollama_llm.OllamaLanguageModel(settings, registry, transcript=None, memory=None, client=None)` — `_chat()` attaches tools at `kwargs["tools"] = self._registry.schemas()`; `run_turn(user_text, execute)` runs the tool loop.
- `autobot.app._build_llm(settings, registry, transcript, memory)` builds the Ollama (or Anthropic) model; `build()` wires everything.
- Test helpers in `tests/unit/test_ollama_llm.py`: `_FakeOllama` (records `self.calls` = list of `chat(**kwargs)` dicts, so `calls[i]["tools"]` is the advertised schema list), `_resp(content, tool_calls)`, `_tc(name, args)`.

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/tools/registry.py` (modify) | Add `ToolSpec.core: bool`; add `ToolRegistry.specs() -> list[ToolSpec]` |
| `src/autobot/config.py` (modify) | Add `tool_budget`, `tool_selection`, `tool_core_extra`, `tool_core_remove` |
| `src/autobot/core/interfaces.py` (modify) | Add `ToolSelector` Protocol |
| `src/autobot/tools/selection.py` (create) | Pure `tokenize`/`score_tools`; `AllToolsSelector`, `LexicalToolSelector`, `build_tool_selector` |
| `src/autobot/llm/ollama_llm.py` (modify) | Inject `selector`; advertise `selector.select(query)` instead of `registry.schemas()` |
| `src/autobot/app.py` (modify) | Build the selector in `_build_llm` and pass it to the Ollama model |
| built-in tool files (modify) | Mark ~14 frequent built-ins `core=True` |
| `tests/unit/test_tools.py` (modify) | `ToolSpec.core` + `registry.specs()` tests |
| `tests/unit/test_config.py` (modify) | New settings defaults + overlay |
| `tests/unit/test_tool_selection.py` (create) | `tokenize`/`score_tools`/selectors/factory tests |
| `tests/unit/test_ollama_llm.py` (modify) | Selector-consulted + no-selector-fallback tests |

---

### Task 1: `ToolSpec.core` field + `ToolRegistry.specs()`

**Files:**
- Modify: `src/autobot/tools/registry.py`
- Test: `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ToolSpec(..., core: bool = False)` (keyword field, last); `ToolRegistry.specs() -> list[ToolSpec]` (lock-guarded snapshot of all registered specs).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tools.py`:

```python
def test_toolspec_core_defaults_false() -> None:
    spec = ToolSpec(name="t", description="", parameters={}, handler=lambda: "")
    assert spec.core is False


def test_toolspec_core_can_be_set() -> None:
    spec = ToolSpec(name="t", description="", parameters={}, handler=lambda: "", core=True)
    assert spec.core is True


def test_registry_specs_returns_all_registered_specs() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="a", description="", parameters={}, handler=lambda: "a"))
    registry.register(
        ToolSpec(name="b", description="", parameters={}, handler=lambda: "b", core=True)
    )
    specs = registry.specs()
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"a", "b"}
    assert by_name["b"].core is True
    assert by_name["a"].core is False
```

(`ToolSpec` and `ToolRegistry` are already imported at the top of `test_tools.py`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tools.py -k "core or specs" -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'core'` and `AttributeError: 'ToolRegistry' object has no attribute 'specs'`.

- [ ] **Step 3: Add the field**

In `src/autobot/tools/registry.py`, in the `ToolSpec` dataclass, add this field immediately after the `network: bool = False` field (keep it last so positional construction is unaffected):

```python
    # True when this tool is part of the always-on "core" set advertised on every
    # turn (the frequent, everyday built-ins). False tools are "gated": advertised
    # only when the ToolSelector judges them relevant to the user's message, which
    # is what keeps per-turn tool context bounded (see autobot.tools.selection). MCP
    # tools are always gated (the adapter never sets this).
    core: bool = False
```

- [ ] **Step 4: Add `specs()`**

In `src/autobot/tools/registry.py`, in `ToolRegistry`, add this method immediately after `schemas()`:

```python
    def specs(self) -> list[ToolSpec]:
        """Return a snapshot of every registered spec (for relevance selection).

        Unlike :meth:`schemas`, this preserves the full :class:`ToolSpec` objects
        (including ``core``/``risk``/``network``), which the :class:`ToolSelector`
        needs to partition core vs. gated and to rank gated tools.
        """
        with self._lock:
            return list(self._tools.values())
```

- [ ] **Step 5: Run tests + mypy**

Run: `uv run pytest tests/unit/test_tools.py -v` → PASS (all, incl. the 3 new).
Run: `uv run mypy` → `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/registry.py tests/unit/test_tools.py
git commit -m "feat(tools): ToolSpec.core flag + ToolRegistry.specs() for relevance selection"
```

---

### Task 2: Tool-selection settings

**Files:**
- Modify: `src/autobot/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.tool_budget: int = 20`, `Settings.tool_selection: str = "lexical"`, `Settings.tool_core_extra: list[str] = []`, `Settings.tool_core_remove: list[str] = []`.

**Context:** `Settings` is `frozen=True, slots=True`; mutable defaults need `field(default_factory=list)`. `config.py` currently imports `from dataclasses import asdict, dataclass, fields, replace` — `field` must be added. `_coerce` returns non-primitive values (lists) unchanged (the trailing `return value`), so list fields overlay correctly from JSON.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py` (it already imports `Settings` and a `write_settings`/`Path` helper — reuse the existing `write_settings` import pattern in that file; if a `_write`/`write_settings(...)` helper is already defined there, mirror it):

```python
def test_tool_selection_defaults() -> None:
    s = Settings()
    assert s.tool_budget == 20
    assert s.tool_selection == "lexical"
    assert s.tool_core_extra == []
    assert s.tool_core_remove == []


def test_tool_selection_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings(
        {
            "tool_budget": 12,
            "tool_selection": "all",
            "tool_core_extra": ["slack__search"],
            "tool_core_remove": ["disk_space"],
        },
        path,
    )
    s = Settings.load(path)
    assert s.tool_budget == 12
    assert s.tool_selection == "all"
    assert s.tool_core_extra == ["slack__search"]
    assert s.tool_core_remove == ["disk_space"]
```

If `test_config.py` does not already import `write_settings`/`Path`, add at the top:
```python
from pathlib import Path

from autobot.config import Settings, write_settings
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k tool -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'tool_budget'`.

- [ ] **Step 3: Add `field` to the dataclass import**

In `src/autobot/config.py`, change:
```python
from dataclasses import asdict, dataclass, fields, replace
```
to:
```python
from dataclasses import asdict, dataclass, field, fields, replace
```

- [ ] **Step 4: Add the settings**

In `src/autobot/config.py`, in the `Settings` dataclass, add this block immediately after the MCP block (right after `allow_mcp: bool = False`, before `# --- daemon (Phase 3c) ---`):

```python
    # --- tool selection / context budget ---
    # Per-turn tool advertising is relevance-gated: a small always-on "core" set
    # plus the top tools the ToolSelector judges relevant to the user's message,
    # bounded by tool_budget. This keeps context (and cost, and a small model's
    # tool-selection accuracy) from growing with the number of registered/MCP tools.
    # tool_selection: "lexical" (on-device keyword ranking, default) or "all"
    # (advertise every tool — the pre-optimization behavior, for debugging).
    tool_budget: int = 20
    tool_selection: str = "lexical"
    # Tool names to force into / out of the core set without code edits.
    tool_core_extra: list[str] = field(default_factory=list)
    tool_core_remove: list[str] = field(default_factory=list)
```

- [ ] **Step 5: Run tests + mypy**

Run: `uv run pytest tests/unit/test_config.py -v` → PASS (all, incl. the 2 new).
Run: `uv run mypy` → `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/config.py tests/unit/test_config.py
git commit -m "feat(config): tool_budget/tool_selection/tool_core_extra/remove settings"
```

---

### Task 3: `ToolSelector` protocol + pure scorer + `AllToolsSelector`

**Files:**
- Modify: `src/autobot/core/interfaces.py`
- Create: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `ToolSpec`, `ToolRegistry`.
- Produces:
  - `ToolSelector` Protocol: `select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]`.
  - `tokenize(text: str) -> list[str]` (pure).
  - `score_tools(query: str, specs: Sequence[ToolSpec]) -> list[tuple[ToolSpec, float]]` (pure; relevance-desc, then name-asc; zero-score specs excluded).
  - `AllToolsSelector(registry)` — returns `registry.specs()` (the "all" mode).

- [ ] **Step 1: Add the `ToolSelector` protocol**

In `src/autobot/core/interfaces.py`, extend the existing `TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    # Imported only for type checking so this module stays runtime-light.
    from autobot.core.types import AudioClip, ToolExecutor, Transcription
    from autobot.tools.registry import ToolSpec
```

Then append this protocol at the end of the file:

```python
@runtime_checkable
class ToolSelector(Protocol):
    """Chooses which tools to advertise to the model for one round.

    The pipeline funnels every request's tool list through a selector instead of
    advertising the whole registry. Implementations return the always-on core set
    plus the gated tools judged relevant to ``query`` (and any explicitly
    ``pinned`` tools), bounded so per-turn tool context stays small.
    """

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return the ToolSpecs to advertise this round.

        Args:
            query: The current user message (the relevance signal).
            pinned: Tool names to force-include (e.g. discovered via an escape
                hatch); resolved against the registry and added to the result.

        Returns:
            A bounded, deduplicated list of specs: core ∪ pinned ∪ top relevant.
        """
        ...
```

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_tool_selection.py`:

```python
"""Tests for pure tool-selection logic (no model, no network)."""

from __future__ import annotations

from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.selection import (
    AllToolsSelector,
    score_tools,
    tokenize,
)


def _spec(name: str, desc: str = "", *, core: bool = False) -> ToolSpec:
    return ToolSpec(name=name, description=desc, parameters={}, handler=lambda: name, core=core)


def test_tokenize_lowercases_drops_short_and_stopwords() -> None:
    assert tokenize("What's MY battery?") == ["what", "battery"]
    assert tokenize("a to of the") == []


def test_score_tools_ranks_relevant_first_and_excludes_zero() -> None:
    battery = _spec("battery_status", "Check the Mac's battery level and charging state.")
    volume = _spec("set_volume", "Set the system output volume.")
    scored = score_tools("what's my battery", [battery, volume])
    assert [s.name for s, _ in scored] == ["battery_status"]  # volume scored 0 → excluded


def test_score_tools_empty_query_returns_empty() -> None:
    assert score_tools("", [_spec("x", "y")]) == []


def test_score_tools_name_match_beats_description_only() -> None:
    # "slack" in the name should outrank a tool that only mentions slack in prose.
    named = _spec("slack__send", "Send a message.")
    prose = _spec("notify", "Posts an update to a slack channel.")
    ranked = [s.name for s, _ in score_tools("send a slack message", [named, prose])]
    assert ranked[0] == "slack__send"


def test_all_tools_selector_returns_everything() -> None:
    reg = ToolRegistry()
    reg.register(_spec("a"))
    reg.register(_spec("b", core=True))
    selector = AllToolsSelector(reg)
    names = {s.name for s in selector.select("anything")}
    assert names == {"a", "b"}
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.tools.selection`.

- [ ] **Step 4: Write the module (scorer + AllToolsSelector)**

Create `src/autobot/tools/selection.py`:

```python
"""On-device tool selection: pick a bounded, relevant subset to advertise.

Advertising every registered tool on every turn bloats context linearly with the
tool count — costly, and (on a small local model) accuracy-degrading once the set
passes a few dozen tools. This module ranks the **gated** tools by a lightweight,
dependency-free keyword relevance (term overlap with IDF weighting and a name-match
boost) and returns the always-on **core** tools plus the top matches, bounded by a
budget. Everything here is pure/synchronous so it is unit-tested without a model.

The ``mcp`` SDK is irrelevant here; this operates only on registered ``ToolSpec``s.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.config import Settings
    from autobot.core.interfaces import ToolSelector
    from autobot.tools.registry import ToolRegistry, ToolSpec

# Words too common to carry intent; dropped before scoring so they don't inflate
# overlap. Deliberately small — the IDF weighting already down-weights frequent terms.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "to", "of", "my", "is", "it", "and", "or", "do", "you",
        "can", "could", "would", "will", "please", "i", "me", "for", "on", "in",
        "with", "this", "that", "your", "what", "whats",
    }
)
_NAME_BOOST = 2.0  # a query term matching the tool *name* counts double


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop short tokens and stopwords."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def score_tools(query: str, specs: Sequence[ToolSpec]) -> list[tuple[ToolSpec, float]]:
    """Rank ``specs`` by keyword relevance to ``query`` (desc, then name asc).

    Each tool's document is its name (split on ``_``) plus its description. Score is
    the IDF-weighted overlap of query terms with the document, with name matches
    boosted. Zero-score specs are excluded — better to advertise fewer tools than to
    pad the context with irrelevant ones. ``query`` with no usable terms → ``[]``.
    """
    q = set(tokenize(query))
    if not q or not specs:
        return []
    docs: list[tuple[ToolSpec, set[str], set[str]]] = []
    df: dict[str, int] = {}
    for s in specs:
        name_tokens = set(tokenize(s.name.replace("_", " ")))
        doc = name_tokens | set(tokenize(s.description))
        docs.append((s, name_tokens, doc))
        for t in doc:
            df[t] = df.get(t, 0) + 1
    n = len(specs)
    scored: list[tuple[ToolSpec, float]] = []
    for s, name_tokens, doc in docs:
        score = 0.0
        for t in q & doc:
            idf = math.log(1 + n / (1 + df[t]))
            score += idf * (_NAME_BOOST if t in name_tokens else 1.0)
        if score > 0:
            scored.append((s, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].name))
    return scored


class AllToolsSelector:
    """Advertises every registered tool (the pre-optimization behavior).

    Used when ``tool_selection == "all"`` — a debugging/comparison escape hatch.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return every registered spec, ignoring ``query``/``pinned``."""
        return self._registry.specs()
```

- [ ] **Step 5: Run tests + mypy**

Run: `uv run pytest tests/unit/test_tool_selection.py -v` → PASS (5).
Run: `uv run mypy` → `Success`.
Run: `uv run python -c "import autobot.tools.selection; print('ok')"` → prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/core/interfaces.py src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): ToolSelector protocol, pure keyword scorer, AllToolsSelector"
```

---

### Task 4: `LexicalToolSelector` + `build_tool_selector` factory

**Files:**
- Modify: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `score_tools`, `ToolRegistry`, `Settings`.
- Produces:
  - `LexicalToolSelector(registry, *, budget: int, core_extra: frozenset[str], core_remove: frozenset[str])` implementing `ToolSelector`.
  - `build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector` — `"all"` → `AllToolsSelector`, else `LexicalToolSelector`.

**Selection rule:** advertised = core (always, every core tool) + top-`K` gated by `score_tools` + resolved `pinned`, deduped (first occurrence wins, order: core, then ranked gated, then pinned). `K = max(0, budget - len(core))`. Effective core names = `{s.name for s in specs if s.core} ∪ core_extra − core_remove`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tool_selection.py`:

```python
import pytest

from autobot.config import Settings
from autobot.tools.selection import LexicalToolSelector, build_tool_selector


def _reg() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("set_volume", "Set the system output volume.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    reg.register(_spec("github__issue", "Create a GitHub issue."))
    return reg


def _lexical(reg: ToolRegistry, *, budget: int = 20) -> LexicalToolSelector:
    return LexicalToolSelector(
        reg, budget=budget, core_extra=frozenset(), core_remove=frozenset()
    )


def test_core_tools_always_advertised() -> None:
    names = {s.name for s in _lexical(_reg()).select("what's my battery")}
    assert {"battery_status", "set_volume"} <= names


def test_gated_tool_appears_only_when_relevant() -> None:
    names = {s.name for s in _lexical(_reg()).select("send a slack message")}
    assert "slack__send" in names
    assert "github__issue" not in names  # irrelevant gated tool excluded


def test_irrelevant_query_advertises_core_only() -> None:
    names = {s.name for s in _lexical(_reg()).select("what's my battery")}
    assert names == {"battery_status", "set_volume"}  # no gated tool matched


def test_budget_caps_gated_additions_core_always_kept() -> None:
    # budget 2 == the 2 core tools → K=0, so a matching gated tool is still dropped.
    names = {s.name for s in _lexical(_reg(), budget=2).select("send a slack message")}
    assert names == {"battery_status", "set_volume"}


def test_pinned_tools_are_force_included() -> None:
    names = {s.name for s in _lexical(_reg()).select("hi", pinned=frozenset({"github__issue"}))}
    assert "github__issue" in names  # forced in despite zero relevance


def test_core_extra_and_remove_apply() -> None:
    reg = _reg()
    selector = LexicalToolSelector(
        reg, budget=20, core_extra=frozenset({"slack__send"}), core_remove=frozenset({"set_volume"})
    )
    names = {s.name for s in selector.select("hi")}
    assert "slack__send" in names      # promoted to core
    assert "set_volume" not in names   # demoted out of core


def test_build_tool_selector_picks_impl() -> None:
    reg = _reg()
    assert isinstance(build_tool_selector(Settings(tool_selection="all"), reg), AllToolsSelector)
    assert isinstance(
        build_tool_selector(Settings(tool_selection="lexical"), reg), LexicalToolSelector
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -k "core or gated or budget or pinned or build" -v`
Expected: FAIL — `ImportError: cannot import name 'LexicalToolSelector'`.

- [ ] **Step 3: Implement the selector + factory**

Append to `src/autobot/tools/selection.py`:

```python
class LexicalToolSelector:
    """Relevance-gated tool advertising via on-device keyword ranking.

    Always advertises the core set; fills the remaining budget with the gated tools
    most relevant to the user's message (per :func:`score_tools`); force-includes any
    pinned tools. ``core_extra``/``core_remove`` adjust the core set from settings
    without code edits. Reads the live registry each call, so MCP tools that connect
    or disconnect are picked up automatically.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        budget: int,
        core_extra: frozenset[str],
        core_remove: frozenset[str],
    ) -> None:
        self._registry = registry
        self._budget = budget
        self._core_extra = core_extra
        self._core_remove = core_remove

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return core ∪ top-K relevant gated ∪ pinned, deduped and budget-bounded."""
        specs = self._registry.specs()
        core_names = ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove
        core = [s for s in specs if s.name in core_names]
        gated = [s for s in specs if s.name not in core_names]

        k = max(0, self._budget - len(core))
        ranked = [s for s, _ in score_tools(query, gated)][:k]

        pinned_specs = [s for s in specs if s.name in pinned and s.name not in core_names]

        chosen: list[ToolSpec] = []
        seen: set[str] = set()
        for s in (*core, *ranked, *pinned_specs):
            if s.name not in seen:
                seen.add(s.name)
                chosen.append(s)
        return chosen


def build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector:
    """Construct the configured selector. ``"all"`` → advertise everything.

    Any value other than ``"all"`` (including the default ``"lexical"`` and the
    Phase-4 ``"embedding"`` placeholder) currently builds the lexical selector.
    """
    if settings.tool_selection == "all":
        return AllToolsSelector(registry)
    return LexicalToolSelector(
        registry,
        budget=settings.tool_budget,
        core_extra=frozenset(settings.tool_core_extra),
        core_remove=frozenset(settings.tool_core_remove),
    )
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_tool_selection.py -v` → PASS (all, incl. the 7 new).
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): LexicalToolSelector (core + budgeted relevance) + factory"
```

---

### Task 5: Wire the selector into `OllamaLanguageModel`

**Files:**
- Modify: `src/autobot/llm/ollama_llm.py`
- Test: `tests/unit/test_ollama_llm.py`

**Interfaces:**
- Consumes: `ToolSelector`, `LexicalToolSelector`.
- Produces: `OllamaLanguageModel(settings, registry, transcript=None, memory=None, client=None, selector=None)`. When `selector` is set, each round advertises `selector.select(current_user_text)`; when `None`, falls back to `registry.schemas()` (unchanged behavior — preserves existing tests).

**Context:** `_chat()` advertises tools at `kwargs["tools"] = self._registry.schemas()` (≈line 292). `run_turn()` knows `user_text`; the loop calls `self._chat(messages)` per round. We thread the round's query via `self._round_query`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_ollama_llm.py`:

```python
from autobot.tools.selection import LexicalToolSelector


def test_selector_gates_advertised_tools() -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="battery_status",
            description="Check the Mac's battery level and charging state.",
            parameters={},
            handler=lambda: "100%",
            core=True,
        )
    )
    reg.register(
        ToolSpec(
            name="slack__send",
            description="Send a message to a Slack channel.",
            parameters={},
            handler=lambda **k: "sent",
        )
    )
    selector = LexicalToolSelector(
        reg, budget=20, core_extra=frozenset(), core_remove=frozenset()
    )
    client = _FakeOllama([_resp(content="100%.")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=selector
    )
    model.run_turn("what's my battery?", lambda c: ToolResult(name=c.name, content=""))
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert "battery_status" in advertised          # core, always advertised
    assert "slack__send" not in advertised          # gated, irrelevant to a battery query


def test_no_selector_advertises_all_tools() -> None:
    client = _FakeOllama([_resp(content="hi")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), _registry(), client=client
    )  # no selector → legacy behavior
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert advertised == {"list_files", "open_path"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_ollama_llm.py -k "selector or all_tools" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'selector'`.

- [ ] **Step 3: Add the `TYPE_CHECKING` import**

In `src/autobot/llm/ollama_llm.py`, change the typing import line:
```python
from typing import Any
```
to:
```python
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autobot.core.interfaces import ToolSelector
```

- [ ] **Step 4: Accept + store the selector**

In `OllamaLanguageModel.__init__`, change the signature to add `selector` (keyword, after `client`):

```python
    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
        client: Any | None = None,
        selector: ToolSelector | None = None,
    ) -> None:
```

Then, immediately after `self._registry = registry`, add:

```python
        self._selector = selector
        self._round_query = ""  # current turn's user text; the relevance signal
```

- [ ] **Step 5: Advertise the selected subset**

In `_chat()`, replace:
```python
        if with_tools:
            kwargs["tools"] = self._registry.schemas()
```
with:
```python
        if with_tools:
            kwargs["tools"] = self._tools_for_round()
```

Then add this helper to the class (e.g. immediately after `_chat`):

```python
    def _tools_for_round(self) -> list[dict[str, Any]]:
        """Schemas to advertise this round: the selector's subset, or all tools.

        With a selector wired, only the relevance-gated subset for this turn's
        message is advertised (bounded context). Without one, every registered tool
        is advertised — the original behavior, kept so existing callers/tests are
        unaffected.
        """
        if self._selector is None:
            return self._registry.schemas()
        return [spec.to_schema() for spec in self._selector.select(self._round_query)]
```

- [ ] **Step 6: Set the round query in `run_turn`**

In `run_turn`, immediately after `user_msg = {"role": "user", "content": user_text}`, add:

```python
        self._round_query = user_text  # relevance signal for tool selection this turn
```

- [ ] **Step 7: Run tests + mypy**

Run: `uv run pytest tests/unit/test_ollama_llm.py -v` → PASS (all, incl. the 2 new; existing loop tests still pass because they build the model without a selector).
Run: `uv run mypy` → `Success`.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/llm/ollama_llm.py tests/unit/test_ollama_llm.py
git commit -m "feat(llm): advertise a relevance-gated tool subset on the Ollama path"
```

---

### Task 6: Mark the core built-in tools

**Files:**
- Modify: built-in tool registration files under `src/autobot/tools/`.
- Test: `tests/unit/test_tools.py`

**Interfaces:** Consumes `ToolSpec.core`. Produces: the frequent, everyday built-ins are `core=True` (always advertised); everything else (including all MCP tools) stays gated.

**Context:** Marking is a keyword arg on the relevant `ToolSpec(...)` constructions. Because the field is keyword-only-by-convention (it has a default), it can be added anywhere in the call. mypy strict will catch any typo (`core=True` on a non-`ToolSpec`, or a misspelled field). The core set below is the everyday, high-frequency actions; rarer ones (uninstall, appearance, sleep, lock, set_wifi, reminder list/complete/delete/update, window min/max/hide, list_apps, set_clipboard, close_website, reveal/open_path, file_io/filesystem tools) stay gated and surface via relevance.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tools.py`:

```python
def test_get_time_is_core() -> None:
    from autobot.tools.builtin import register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    spec = reg.get("get_time")
    assert spec is not None
    assert spec.core is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_tools.py -k get_time_is_core -v`
Expected: FAIL — `assert False is True` (get_time not yet marked core).

- [ ] **Step 3: Mark the core built-ins**

Add `core=True` to the `ToolSpec(...)` whose `name` matches each of the following. Pattern — find the construction and add the kwarg, e.g. in `src/autobot/tools/builtin.py`:

```python
    registry.register(
        ToolSpec(
            name="get_time",
            description=...,   # unchanged
            parameters=...,    # unchanged
            handler=...,       # unchanged
            core=True,         # <-- add
        )
    )
```

Mark these names `core=True` (grep each name to find its `ToolSpec`):

| File | Tool names to mark `core=True` |
|---|---|
| `builtin.py` | `get_time` |
| `system.py` | `battery_status`, `wifi_status`, `disk_space` |
| `toggles.py` | `set_volume`, `set_brightness` |
| `apps.py` | `open_app`, `focus_app`, `quit_app` |
| `clipboard.py` | `read_clipboard` |
| `reminders.py` | `create_reminder` |
| `files.py` | `search_files` |
| `web.py` | `web_search`, `open_website` |

(14 tools. Leave every other built-in and every MCP tool unmarked → gated.)

- [ ] **Step 4: Run the test + format + mypy**

Run: `uv run pytest tests/unit/test_tools.py -k get_time_is_core -v` → PASS.
Run: `uv run ruff format .` then `uv run mypy` → `Success` (mypy confirms every `core=True` landed on a real `ToolSpec` field).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/builtin.py src/autobot/tools/system.py src/autobot/tools/toggles.py src/autobot/tools/apps.py src/autobot/tools/clipboard.py src/autobot/tools/reminders.py src/autobot/tools/files.py src/autobot/tools/web.py tests/unit/test_tools.py
git commit -m "feat(tools): mark the frequent everyday built-ins as core (always advertised)"
```

---

### Task 7: Wire the selector into `build()` / `_build_llm`

**Files:**
- Modify: `src/autobot/app.py`
- Test: `tests/unit/test_app_selector.py` (create)

**Interfaces:** Consumes `build_tool_selector`, `OllamaLanguageModel(..., selector=...)`. Produces: in the real app, the Ollama model is built with a selector derived from settings + the live registry. The Anthropic path is unchanged in Phase 1 (handled in Phase 3).

**Context:** `_build_llm(settings, registry, transcript, memory)` (≈line 265) returns the Anthropic model (≈line 281) or `OllamaLanguageModel(settings, registry, transcript, memory=memory)` (≈line 297). `build()` calls it via `ReloadableLanguageModel(lambda: _build_llm(Settings.load(), registry, transcript, memory))` (≈line 579), so the selector is rebuilt (cheap — it just wraps the registry) on each settings reload.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_app_selector.py`:

```python
"""The composition root wires a tool selector into the local LLM."""

from __future__ import annotations

from autobot.app import _build_llm
from autobot.config import Settings
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry
from autobot.tools.selection import AllToolsSelector, LexicalToolSelector


def test_build_llm_wires_lexical_selector_by_default() -> None:
    # context_tokens set so __init__ does not probe a live Ollama server.
    model = _build_llm(Settings(context_tokens=4096), ToolRegistry(), NullTranscript(), None)
    assert isinstance(model, OllamaLanguageModel)
    assert isinstance(model._selector, LexicalToolSelector)


def test_build_llm_honors_tool_selection_all() -> None:
    model = _build_llm(
        Settings(context_tokens=4096, tool_selection="all"), ToolRegistry(), NullTranscript(), None
    )
    assert isinstance(model, OllamaLanguageModel)
    assert isinstance(model._selector, AllToolsSelector)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_app_selector.py -v`
Expected: FAIL — `model._selector` is `None` (selector not yet wired).

- [ ] **Step 3: Build the selector and pass it to the Ollama model**

In `src/autobot/app.py`, in `_build_llm`, replace the final return:
```python
    return OllamaLanguageModel(settings, registry, transcript, memory=memory)
```
with:
```python
    from autobot.tools.selection import build_tool_selector

    selector = build_tool_selector(settings, registry)
    return OllamaLanguageModel(settings, registry, transcript, memory=memory, selector=selector)
```

(Leave the Anthropic branch at ≈line 281 unchanged — Phase 3 wires the cloud path.)

- [ ] **Step 4: Run tests + import smoke + mypy**

Run: `uv run pytest tests/unit/test_app_selector.py -v` → PASS (2).
Run: `uv run python -c "from autobot.app import build; print('build-import-ok')"` → prints `build-import-ok`.
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS (ruff + ruff-format + mypy strict + full pytest suite all green).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/app.py tests/unit/test_app_selector.py
git commit -m "feat(app): wire the tool selector into the local LLM via _build_llm"
```

---

## Manual smoke-test (after Task 7)

Optional, with a live Ollama and `allow_mcp` + an MCP server enabled:

1. `make run`, then ask **"hi"** — confirm the reply is normal. Check `~/.autobot/logs/autobot.log` / the context line: baseline should be far smaller than before (system prompt + ~14 core tools, no gated/MCP schemas).
2. Ask **"what's my battery?"** with MCP servers connected — confirm it answers, and that MCP tool schemas are *not* advertised (battery is core; nothing Slack/GitHub gets sent).
3. Ask something that needs a gated/MCP tool by its words (e.g. "search my files for X", or an enabled MCP server's domain) — confirm the relevant tool is found and called. (If a paraphrase misses, that is the gap Phase 2's `find_tools` escape hatch closes.)
4. Set `"tool_selection": "all"` in `~/.autobot/settings.json`, restart, and confirm the old full-catalog behavior returns (sanity check / comparison).

---

## Self-Review

**1. Spec coverage** (design §4 P1: "`ToolSpec.core` + tiering, `LexicalToolSelector`, `tool_budget`, config fields, wired into the Ollama client; mark the core built-ins; MCP stays gated"):
- `ToolSpec.core` + `registry.specs()` → Task 1 ✓.
- Config fields (`tool_budget`, `tool_selection`, `tool_core_extra`, `tool_core_remove`) → Task 2 ✓.
- `ToolSelector` protocol + pure scorer + `AllToolsSelector` → Task 3 ✓.
- `LexicalToolSelector` (core always + budgeted relevance + pinned + extra/remove) + factory → Task 4 ✓.
- Wired into Ollama client with legacy fallback → Task 5 ✓.
- Core built-ins marked; MCP untouched (adapter never sets `core`) → Task 6 ✓.
- Wired in `build()`/`_build_llm`; Anthropic path deferred to P3 → Task 7 ✓.
- Success criteria (hi/battery shrink; +0 per MCP server; ~20 tools) → achieved by core (~14) + budget (20) + MCP-always-gated; verified by Task 5 (gated exclusion), Task 6 (core marked), and the manual smoke.
- `pinned` is plumbed through the protocol/selector now (default empty) so Phase 2's `find_tools` adds the escape hatch without a signature change.

**2. Placeholder scan:** none — every code step shows complete, runnable code; every run step states the command and expected result. The Task-6 table lists exact tool names (the only "find the construction" step), with a worked before/after example and mypy as the safety net.

**3. Type consistency:**
- `ToolSelector.select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]` — identical in the protocol (Task 3), `AllToolsSelector` (Task 3), `LexicalToolSelector` (Task 4), and the Ollama call site `self._selector.select(self._round_query)` (Task 5).
- `score_tools(query: str, specs: Sequence[ToolSpec]) -> list[tuple[ToolSpec, float]]` and `tokenize(text: str) -> list[str]` — consistent between Task 3 definition and Task 4 usage.
- `build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector` — consistent between Task 4 and the Task 7 call site.
- `OllamaLanguageModel(..., selector: ToolSelector | None = None)` — consistent between Task 5 (definition) and Task 7 (`selector=selector`).
- `ToolRegistry.specs() -> list[ToolSpec]` — defined Task 1, consumed by both selectors (Tasks 3–4).
- `ToolSpec.core: bool` — defined Task 1, set Task 6, read by `LexicalToolSelector` (Task 4) and tests.

No issues found.
