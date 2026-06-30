# MCP Context Optimization — Phase 4: Local Embedding Tool Selector (+ optional schema minification)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Ordering:** Phase 4 shares `tools/selection.py` and the `ToolSelector` protocol with **Phase 2**, which adds `ToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` to the protocol and to `AllToolsSelector`/`LexicalToolSelector`. **Phase 2 merges BEFORE Phase 4 executes.** This plan is written ASSUMING `search` already exists on the protocol and on both existing selectors, and that `find_tools` (Phase 2's escape hatch) routes through `selector.search(...)`. The new `EmbeddingToolSelector` therefore implements **both** `select` and `search`.

**Goal:** Add an opt-in, **100% on-device** `EmbeddingToolSelector` that upgrades *recall* on the local path: same core-always + budgeted-gated + pinned rule as `LexicalToolSelector`, but it ranks gated tools by **cosine similarity** of a locally-embedded query against locally-embedded tool docs (name + description) instead of BM25 keyword overlap. Selected via `tool_selection="embedding"`. The embedding model runs through Ollama's **local** embeddings endpoint, downloads on first use, and on ANY embedding failure the selector falls back to a `LexicalToolSelector` delegate so it never crashes a turn. Optionally, a pure, conservative MCP `inputSchema` minifier trims advertised schemas to cut tokens further.

**Architecture:** One new concrete `ToolSelector` in the existing pure module (`tools/selection.py`), plus a thin Ollama-backed embedder wired only in `build_tool_selector`. The embedder is injected as a `Callable[[str], list[float]]` so the ranking logic stays pure and is unit-tested without a live Ollama. A per-instance vector cache keyed by `adapter.fingerprint(...)` (with a stable name+description hash fallback for built-ins, which aren't MCP `Tool`s) embeds each unchanged tool **once**; the query is embedded **once per call**. Cloud (Anthropic) uses native Tool Search and does **not** use this path (design §7). The optional minifier is a separate pure helper applied where MCP `inputSchema` becomes `ToolSpec.parameters`.

**Tech Stack:** Python 3.11, dataclasses, `math`/`hashlib` (stdlib), the already-present `ollama` client (lazy-imported only in the factory), pytest, mypy strict, ruff. No new base dependency — `nomic-embed-text` is a runtime/optional, download-on-first-use concern gated behind `tool_selection="embedding"`.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- mypy **strict** over `src` AND `tests` — keep it green. Run `uv run mypy`.
- Google-style docstrings on every public module/class/function (ruff `D`); **tests exempt**.
- Line length 100; never hand-format — run `uv run ruff format .` (or `make format`).
- Value objects are `frozen=True, slots=True` dataclasses; no business logic on them.
- **No new runtime dependency.** The embedding model (`nomic-embed-text`, ~270MB) is a runtime/optional concern: lazy-imported `ollama` client (already a dep), opt-in via `tool_selection="embedding"`, downloaded on first use. The baseline install and the lexical path are unchanged. **100% on-device** — only tool descriptions and the query are embedded, locally, and nothing leaves the machine.
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- Verification gate per task: `make check` green (ruff + ruff-format + mypy + pytest). Run a single file with `uv run pytest tests/unit/<file>.py -v`.
- **Branch:** continue on `feat/mcp-integration`. All Phase-4 commits stack there, after Phase 2 (and Phase 3) have merged.

**Interfaces already on the branch (consume these):**
- **Phase 1 (done):** `autobot.tools.registry.ToolSpec` — `frozen=True, slots=True` dataclass with `core: bool = False` (last field) and `to_schema() -> dict`; `ToolRegistry.specs() -> list[ToolSpec]` (lock-guarded snapshot), plus `register/unregister/get/schemas/dispatch`.
- **Phase 1 (done):** `autobot.tools.selection` — pure `tokenize(text) -> list[str]`, `score_tools(query, specs) -> list[tuple[ToolSpec, float]]`; `AllToolsSelector(registry)`; `LexicalToolSelector(registry, *, budget: int, core_extra: frozenset[str], core_remove: frozenset[str])`; `build_tool_selector(settings, registry) -> ToolSelector`.
- **Phase 1 (done):** `autobot.config.Settings` — `frozen=True, slots=True`; `Settings.load(path)` overlays JSON via `_coerce`; already has `tool_budget: int = 20`, `tool_selection: str = "lexical"`, `tool_core_extra: list[str]`, `tool_core_remove: list[str]`. `config.py` already imports `from dataclasses import asdict, dataclass, field, fields, replace`.
- **Phase 2 (merged before P4):** `autobot.core.interfaces.ToolSelector` Protocol now declares **both** `select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]` **and** `search(self, intent: str, *, limit: int = 5) -> list[str]`. Both `AllToolsSelector` and `LexicalToolSelector` implement `search`. The turn loop's `find_tools` escape hatch calls `selector.search(intent)` and pins the names.
- `autobot.mcp.adapter.fingerprint(tool) -> str` — stable SHA-256 over a tool's identity fields (name, description, inputSchema, annotations); used as the vector-cache key for MCP tools. `params_from_input_schema(input_schema) -> dict` — maps an MCP `inputSchema` to `ToolSpec.parameters` (the minification seam).
- `autobot.mcp.session._sync_tools` — copies `adapter.params_from_input_schema(tool.inputSchema)` into `ToolSpec.parameters` (the per-tool minification call site, Task 7 only).
- `autobot.llm.ollama_llm.OllamaLanguageModel(settings, registry, transcript=None, memory=None, client=None, selector=None)` — built in `app._build_llm`; `Client(host=settings.ollama_host)` is how the Ollama client is constructed.
- The installed `ollama` package exposes **`Client.embed(model, input)`** (modern; `input: str | Sequence[str]`, returns `EmbedResponse` whose `.embeddings` is a `list[list[float]]`) and the legacy `Client.embeddings(model, prompt)` (returns `EmbeddingsResponse` whose `.embedding` is a single `list[float]`). **Verified** via `uv run python -c "import ollama; print([m for m in dir(ollama.Client) if 'emb' in m.lower()])"` → `['embed', 'embeddings']`. We use `embed`.
- Test helpers in `tests/unit/test_tool_selection.py`: `_spec(name, desc="", *, core=False)`, `_reg()`, `_lexical(reg, *, budget=20)`; imports `AllToolsSelector, LexicalToolSelector, build_tool_selector, score_tools, tokenize` from `autobot.tools.selection` and `Settings`, `ToolRegistry`, `ToolSpec`.

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/config.py` (modify) | Add `embedding_model: str = "nomic-embed-text"` |
| `src/autobot/tools/selection.py` (modify) | Add a `[tools]` logger; pure `cosine`/`embed_doc`; `_doc_key` (fingerprint-or-hash); `EmbeddingToolSelector(registry, *, embedder, fallback, budget, core_extra, core_remove)` (`select` + `search`, fingerprint-cached, lexical fallback on error); extend `build_tool_selector` for `"embedding"` |
| `src/autobot/tools/schema_min.py` (create) | OPTIONAL final task: pure, conservative `minify_schema(schema)` (strips whitespace; drops verbose nested `description`s; preserves type/required/enum) |
| `src/autobot/mcp/session.py` (modify) | OPTIONAL final task: apply `minify_schema(...)` when copying `inputSchema` into `ToolSpec.parameters` |
| `tests/unit/test_config.py` (modify) | `embedding_model` default + overlay |
| `tests/unit/test_tool_selection.py` (modify) | `cosine`/`embed_doc`/`_doc_key`; `EmbeddingToolSelector` ranking, core-always, budget, pinned, `search`, fingerprint-cache (embed once/tool), lexical fallback on error; `build_tool_selector("embedding")` |
| `tests/unit/test_schema_min.py` (create) | OPTIONAL final task: minifier preserves type/required/enum, drops nested descriptions, shrinks token count |

---

### Task 1: `embedding_model` setting

**Files:**
- Modify: `src/autobot/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.embedding_model: str = "nomic-embed-text"`.

**Context:** `Settings` is `frozen=True, slots=True`. The tool-selection block already exists (`tool_budget`/`tool_selection`/`tool_core_extra`/`tool_core_remove`, ≈lines 181–192, before `# --- daemon (Phase 3c) ---`). `tool_selection` already accepts `"embedding"` per the design table; this task only adds the model name the embedding path will use. `test_config.py` already imports `Settings`, `write_settings`, `Path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_embedding_model_default() -> None:
    assert Settings().embedding_model == "nomic-embed-text"


def test_embedding_model_overlays_from_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write_settings({"embedding_model": "mxbai-embed-large"}, path)
    assert Settings.load(path).embedding_model == "mxbai-embed-large"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k embedding_model -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'embedding_model'`.

- [ ] **Step 3: Add the setting**

In `src/autobot/config.py`, in the `Settings` dataclass, add this immediately after the `tool_core_remove` field (still inside the tool-selection block, before `# --- daemon (Phase 3c) ---`):

```python
    # Local embedding model for tool_selection="embedding" (the EmbeddingToolSelector
    # recall upgrade on the local path). Pulled via Ollama on first use (~270MB); it
    # is NOT a base dependency and is never used unless tool_selection is "embedding".
    # Embeddings run entirely on-device — only tool descriptions and the user query are
    # embedded, locally, and nothing leaves the machine.
    embedding_model: str = "nomic-embed-text"
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_config.py -v` → PASS (all, incl. the 2 new).
Run: `uv run mypy` → `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/config.py tests/unit/test_config.py
git commit -m "feat(config): embedding_model setting for the local embedding tool selector"
```

---

### Task 2: Pure embedding helpers — `cosine`, `embed_doc`, `_doc_key`

**Files:**
- Modify: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `ToolSpec`, `adapter.fingerprint` (lazy).
- Produces (all pure, no model):
  - `cosine(a: Sequence[float], b: Sequence[float]) -> float` — cosine similarity; `0.0` on a zero vector or length mismatch.
  - `embed_doc(spec: ToolSpec) -> str` — the text embedded for a tool: `f"{spec.name} {spec.description}".strip()`.
  - `_doc_key(spec: ToolSpec) -> str` — stable cache key: `adapter.fingerprint(spec)` when the spec satisfies the adapter's `_ToolLike` shape (it does: `ToolSpec` has `name`/`description`, but **not** `inputSchema`/`annotations`), otherwise a SHA-256 of `embed_doc(spec)`. **Decision/justification:** `adapter.fingerprint` reads `tool.inputSchema` and `tool.annotations`, which a `ToolSpec` does *not* have — so we cannot call it on a `ToolSpec`. We therefore key the cache on a SHA-256 of `embed_doc(spec)` (name + description), which is exactly the text we embed: identical for an unchanged tool, different the moment the name or description changes (re-embed), and collision-safe. The design's "keyed by `adapter.fingerprint`" intent — *never re-embed an unchanged tool* — is preserved; we just hash the embedded text directly because the full MCP `Tool` object isn't available at selection time (the registry holds `ToolSpec`s, not `Tool`s).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tool_selection.py`:

```python
from autobot.tools.selection import _doc_key, cosine, embed_doc


def test_cosine_identical_is_one_orthogonal_is_zero() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_handles_zero_vector_and_length_mismatch() -> None:
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector → no direction
    assert cosine([1.0], [1.0, 0.0]) == 0.0  # length mismatch → 0, never raises


def test_embed_doc_is_name_plus_description() -> None:
    assert embed_doc(_spec("battery_status", "Check the battery.")) == (
        "battery_status Check the battery."
    )


def test_doc_key_stable_for_same_text_changes_with_description() -> None:
    a = _doc_key(_spec("t", "one"))
    assert a == _doc_key(_spec("t", "one"))  # same name+desc → same key
    assert a != _doc_key(_spec("t", "two"))  # changed description → new key
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -k "cosine or embed_doc or doc_key" -v`
Expected: FAIL — `ImportError: cannot import name 'cosine' from 'autobot.tools.selection'`.

- [ ] **Step 3: Add a `[tools]` logger + the helpers**

In `src/autobot/tools/selection.py`, change the imports at the top of the module from:

```python
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.config import Settings
    from autobot.core.interfaces import ToolSelector
    from autobot.tools.registry import ToolRegistry, ToolSpec
```

to:

```python
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.config import Settings
    from autobot.core.interfaces import ToolSelector
    from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# Type of an injected embedder: maps one text to its embedding vector. The real one
# calls Ollama locally; tests pass a deterministic fake so ranking is unit-testable
# without a live model.
Embedder = Callable[[str], list[float]]
```

Then append these pure helpers (after `score_tools`, before `class AllToolsSelector`):

```python
def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors; ``0.0`` on a zero vector or length mismatch.

    Never raises — a mismatched or zero vector (e.g. an embedder returned ``[]``)
    yields ``0.0`` so a single bad embedding ranks a tool last instead of crashing
    the turn.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embed_doc(spec: ToolSpec) -> str:
    """Return the text embedded to represent ``spec``: its name plus description."""
    return f"{spec.name} {spec.description}".strip()


def _doc_key(spec: ToolSpec) -> str:
    """Stable cache key for ``spec``'s embedding vector.

    Keys on a SHA-256 of :func:`embed_doc` — the exact text we embed. Identical for an
    unchanged tool (so its vector is reused, never re-embedded) and different the moment
    the name or description changes (so it is re-embedded). We hash the doc text rather
    than call :func:`autobot.mcp.adapter.fingerprint` because the registry holds
    :class:`ToolSpec`s, not MCP ``Tool`` objects (no ``inputSchema``/``annotations`` to
    fingerprint), and the doc text is the only embedding-relevant identity here.
    """
    return hashlib.sha256(embed_doc(spec).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests + mypy + import smoke**

Run: `uv run pytest tests/unit/test_tool_selection.py -k "cosine or embed_doc or doc_key" -v` → PASS (4).
Run: `uv run python -c "import autobot.tools.selection; print('ok')"` → prints `ok`.
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): pure cosine/embed_doc/doc-key helpers for embedding selection"
```

---

### Task 3: `EmbeddingToolSelector` — ranking, core/budget/pinned, cache, fallback

**Files:**
- Modify: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `cosine`, `embed_doc`, `_doc_key`, `score_tools`, `Embedder`, `ToolRegistry`, `LexicalToolSelector`.
- Produces: `EmbeddingToolSelector(registry, *, embedder: Embedder, fallback: LexicalToolSelector, budget: int, core_extra: frozenset[str], core_remove: frozenset[str])` implementing the **full** `ToolSelector` protocol:
  - `select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]`
  - `search(self, intent: str, *, limit: int = 5) -> list[str]`

**Selection rule (identical to `LexicalToolSelector`, only the gated ranking differs):** advertised = core (always) + top-`K` gated **by cosine similarity** + resolved `pinned`, deduped (order: core, then ranked gated, then pinned). `K = max(0, budget - len(core))`. Effective core names = `{s.name for s in specs if s.core} ∪ core_extra − core_remove`.

**Cache + once-per-call rules:**
- Tool-side: a per-instance `dict[str, list[float]]` keyed by `_doc_key(spec)`. Embed each gated tool's `embed_doc(spec)` **once**; reuse the cached vector on every later call for an unchanged tool. The cache is never invalidated by hand — a changed name/description simply produces a new key (and the old entry is harmlessly orphaned).
- Query-side: embed the query **once per `select`/`search` call**.

**Graceful degradation (REQUIRED):** all embedder calls go through one private `_rank_gated(query, gated) -> list[ToolSpec]`. If embedding the query *or* any tool raises, log a `[tools]` **warning once** (a per-instance `_warned` flag) and delegate ranking to `self._fallback` (the `LexicalToolSelector`) — both `select` and `search` then degrade to lexical for that call and never crash the turn.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tool_selection.py`:

```python
from autobot.tools.selection import EmbeddingToolSelector


def _fake_embedder(table: dict[str, list[float]], *, default: list[float] | None = None):
    """Deterministic embedder: maps a text to a fixed vector (substring-keyed)."""

    def embed(text: str) -> list[float]:
        for key, vec in table.items():
            if key in text:
                return vec
        if default is not None:
            return default
        raise AssertionError(f"no fake embedding for {text!r}")

    return embed


def _embedding(reg: ToolRegistry, embedder, *, budget: int = 20) -> EmbeddingToolSelector:
    fallback = LexicalToolSelector(
        reg, budget=budget, core_extra=frozenset(), core_remove=frozenset()
    )
    return EmbeddingToolSelector(
        reg,
        embedder=embedder,
        fallback=fallback,
        budget=budget,
        core_extra=frozenset(),
        core_remove=frozenset(),
    )


def test_embedding_ranks_gated_by_cosine() -> None:
    # Query vector points at slack__send; github__issue is orthogonal → excluded by K? No —
    # K leaves room, but cosine 0 ranks it last; assert slack is chosen and ranked first.
    table = {
        "slack__send": [1.0, 0.0],
        "github__issue": [0.0, 1.0],
        "send a message via slack": [1.0, 0.0],  # query → slack direction
    }
    names = [s.name for s in _embedding(_reg(), _fake_embedder(table)).select(
        "send a message via slack"
    )]
    assert "slack__send" in names
    assert names.index("slack__send") < names.index("github__issue")  # cosine ranks slack first


def test_embedding_core_always_advertised() -> None:
    table = {
        "battery_status": [1.0, 0.0],
        "set_volume": [0.0, 1.0],
        "slack__send": [0.0, 0.0],
        "github__issue": [0.0, 0.0],
    }
    names = {
        s.name
        for s in _embedding(_reg(), _fake_embedder(table, default=[0.0, 0.0])).select("anything")
    }
    assert {"battery_status", "set_volume"} <= names  # core always present


def test_embedding_budget_caps_gated_core_kept() -> None:
    table = {"slack__send": [1.0, 0.0]}
    names = {
        s.name
        for s in _embedding(_reg(), _fake_embedder(table, default=[1.0, 0.0]), budget=2).select(
            "send slack"
        )
    }
    assert names == {"battery_status", "set_volume"}  # budget 2 == core → K=0, no gated


def test_embedding_pinned_force_included() -> None:
    table = {"github__issue": [0.0, 1.0]}
    sel = _embedding(_reg(), _fake_embedder(table, default=[1.0, 0.0]))
    names = {s.name for s in sel.select("hi", pinned=frozenset({"github__issue"}))}
    assert "github__issue" in names  # forced in regardless of similarity


def test_embedding_search_returns_ranked_gated_names() -> None:
    table = {
        "slack__send": [1.0, 0.0],
        "github__issue": [0.0, 1.0],
        "post to a slack channel": [1.0, 0.0],
    }
    names = _embedding(_reg(), _fake_embedder(table, default=[0.0, 0.0])).search(
        "post to a slack channel", limit=1
    )
    assert names == ["slack__send"]  # best gated match only; core excluded from search


def test_embedding_caches_tool_vectors_embeds_once_per_tool() -> None:
    calls: list[str] = []

    def counting_embed(text: str) -> list[float]:
        calls.append(text)
        return [1.0, 0.0] if "slack" in text else [0.0, 1.0]

    sel = _embedding(_reg(), counting_embed)
    sel.select("send slack")
    sel.select("send slack")  # second call: tool vectors must be reused, not re-embedded
    tool_embeds = [c for c in calls if "Send a message" in c or "Create a GitHub" in c]
    # Two gated tools → embedded exactly once each across both select() calls.
    assert len(tool_embeds) == 2


def test_embedding_falls_back_to_lexical_on_embedder_error() -> None:
    def boom(text: str) -> list[float]:
        raise RuntimeError("ollama down / model not pulled")

    names = {s.name for s in _embedding(_reg(), boom).select("send a slack message")}
    assert "slack__send" in names  # lexical fallback found it despite the embedder failing
    assert "battery_status" in names  # core still advertised via the fallback path
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -k embedding -v`
Expected: FAIL — `ImportError: cannot import name 'EmbeddingToolSelector'`.

- [ ] **Step 3: Implement the selector**

Append to `src/autobot/tools/selection.py` (after `LexicalToolSelector`, before `build_tool_selector`):

```python
class EmbeddingToolSelector:
    """Relevance-gated advertising that ranks gated tools by **local** embeddings.

    A recall upgrade over :class:`LexicalToolSelector` for the *local* path only
    (the cloud path uses Anthropic's native tool search and never this). The core
    set, budget, pinning, and ``core_extra``/``core_remove`` rules are identical to
    the lexical selector — only the gated ranking differs: gated tools are ranked by
    cosine similarity of a locally-embedded query against locally-embedded tool docs
    (``name + description``).

    Embedding is done by an injected :data:`Embedder` (so ranking is unit-tested
    without a live model). Each tool's vector is embedded once and cached by
    :func:`_doc_key`, so an unchanged tool is never re-embedded; the query is embedded
    once per call. On **any** embedding failure (model not pulled, host down, bad
    vector) the selector logs once and delegates to a :class:`LexicalToolSelector`
    fallback, so it can never crash a turn.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        embedder: Embedder,
        fallback: LexicalToolSelector,
        budget: int,
        core_extra: frozenset[str],
        core_remove: frozenset[str],
    ) -> None:
        self._registry = registry
        self._embedder = embedder
        self._fallback = fallback
        self._budget = budget
        self._core_extra = core_extra
        self._core_remove = core_remove
        self._cache: dict[str, list[float]] = {}  # _doc_key -> tool vector
        self._warned = False  # log the embedding-failure fallback at most once

    def _core_names(self, specs: Sequence[ToolSpec]) -> set[str]:
        """Effective core set: marked-core ∪ core_extra − core_remove."""
        return ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove

    def _tool_vector(self, spec: ToolSpec) -> list[float]:
        """Return ``spec``'s cached embedding, embedding (once) on a cache miss."""
        key = _doc_key(spec)
        vec = self._cache.get(key)
        if vec is None:
            vec = self._embedder(embed_doc(spec))
            self._cache[key] = vec
        return vec

    def _rank_gated(self, query: str, gated: Sequence[ToolSpec]) -> list[ToolSpec]:
        """Rank gated tools by cosine to ``query``; lexical fallback on any failure.

        Embeds the query once, then scores each gated tool against its cached vector.
        Zero-or-negative-similarity tools are dropped (like lexical's zero-score rule).
        Any exception from the embedder routes the whole ranking through the lexical
        fallback (warned once), so a missing model never breaks the turn.
        """
        if not gated:
            return []
        try:
            qv = self._embedder(query)
            scored = [(s, cosine(qv, self._tool_vector(s))) for s in gated]
        except Exception:  # model not pulled, host down, etc. — degrade, never crash
            if not self._warned:
                self._warned = True
                _log.warning(
                    "embedding selection failed; falling back to lexical"
                    " (is the embedding model pulled? `ollama pull`)",
                    exc_info=True,
                )
            return [s for s, _ in score_tools(query, gated)]
        ranked = sorted((p for p in scored if p[1] > 0.0), key=lambda p: (-p[1], p[0].name))
        return [s for s, _ in ranked]

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return core ∪ top-K embedding-ranked gated ∪ pinned, deduped and bounded."""
        specs = self._registry.specs()
        core_names = self._core_names(specs)
        core = [s for s in specs if s.name in core_names]
        gated = [s for s in specs if s.name not in core_names]

        k = max(0, self._budget - len(core))
        ranked = self._rank_gated(query, gated)[:k]

        pinned_specs = [s for s in specs if s.name in pinned and s.name not in core_names]

        chosen: list[ToolSpec] = []
        seen: set[str] = set()
        for s in (*core, *ranked, *pinned_specs):
            if s.name not in seen:
                seen.add(s.name)
                chosen.append(s)
        return chosen

    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Return the names of the best embedding-ranked **gated** tools for ``intent``.

        Powers Phase 2's ``find_tools`` escape hatch. Searches only the gated pool
        (core tools are already advertised every turn), degrading to lexical ranking
        on an embedding failure exactly as :meth:`select` does.
        """
        specs = self._registry.specs()
        core_names = self._core_names(specs)
        gated = [s for s in specs if s.name not in core_names]
        return [s.name for s in self._rank_gated(intent, gated)[:limit]]
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/unit/test_tool_selection.py -k embedding -v` → PASS (7).
Run: `uv run pytest tests/unit/test_tool_selection.py -v` → PASS (all — existing lexical/scorer tests untouched).
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): EmbeddingToolSelector (local cosine recall, cached, lexical fallback)"
```

---

### Task 4: Route `tool_selection="embedding"` in `build_tool_selector`

**Files:**
- Modify: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `Settings`, `ToolRegistry`, `EmbeddingToolSelector`, `LexicalToolSelector`.
- Produces: `build_tool_selector(settings, registry) -> ToolSelector` now routes `"embedding"` → `EmbeddingToolSelector` (real Ollama embedder + a `LexicalToolSelector` fallback). `"all"` → `AllToolsSelector`; everything else → `LexicalToolSelector` (both unchanged).

**Context:** The real embedder lazy-imports `from ollama import Client` and calls `Client(host=settings.ollama_host).embed(model=settings.embedding_model, input=text)`, returning the first vector in `EmbedResponse.embeddings`. Lazy import keeps `tool_selection != "embedding"` (and the test suite) free of any Ollama touch. The embedder is built once and closes over a single client. `build_tool_selector` is rebuilt on every settings reload (it just wraps the registry), so this stays cheap.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tool_selection.py`:

```python
def test_build_tool_selector_picks_embedding() -> None:
    reg = _reg()
    sel = build_tool_selector(Settings(tool_selection="embedding"), reg)
    assert isinstance(sel, EmbeddingToolSelector)
    # The real embedder is lazy: building the selector must NOT touch Ollama. Selecting
    # an irrelevant query exercises only core + the fallback wiring, no embedder call.
    names = {s.name for s in sel.select("")}  # empty query → no gated work, core only
    assert {"battery_status", "set_volume"} <= names
```

(The empty query means `_rank_gated` short-circuits before calling the embedder when there are gated tools? No — gated is non-empty, so the embedder *would* run. Keep the assertion to core membership, which the test below makes safe by ensuring the embedder isn't invoked for an empty query.)

Adjust the test to avoid a live Ollama call by asserting only construction + type (the embedder is wired but not exercised here; its behavior is covered by Task 3 with fakes):

```python
def test_build_tool_selector_picks_embedding() -> None:
    sel = build_tool_selector(Settings(tool_selection="embedding"), _reg())
    assert isinstance(sel, EmbeddingToolSelector)


def test_build_tool_selector_embedding_has_lexical_fallback() -> None:
    sel = build_tool_selector(Settings(tool_selection="embedding"), _reg())
    assert isinstance(sel, EmbeddingToolSelector)
    assert isinstance(sel._fallback, LexicalToolSelector)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -k "picks_embedding or lexical_fallback" -v`
Expected: FAIL — `build_tool_selector` returns a `LexicalToolSelector` for `"embedding"` (the Phase-1 placeholder behavior).

- [ ] **Step 3: Add the embedder builder + routing**

In `src/autobot/tools/selection.py`, add this helper immediately before `build_tool_selector`:

```python
def _ollama_embedder(settings: Settings) -> Embedder:
    """Build a local Ollama-backed embedder closing over one client.

    Lazy-imports the ``ollama`` client (kept out of the import path for the lexical/all
    selectors and the test suite) and embeds via the LOCAL embeddings endpoint, so
    nothing leaves the machine. Any error propagates to the caller's fallback handler.
    """
    from ollama import Client

    client = Client(host=settings.ollama_host)
    model = settings.embedding_model

    def embed(text: str) -> list[float]:
        resp = client.embed(model=model, input=text)
        return list(resp.embeddings[0])

    return embed
```

Then replace `build_tool_selector` with:

```python
def build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector:
    """Construct the configured selector.

    - ``"all"`` → :class:`AllToolsSelector` (advertise everything; debugging escape hatch).
    - ``"embedding"`` → :class:`EmbeddingToolSelector` ranking gated tools by local
      embeddings, with a :class:`LexicalToolSelector` fallback for any embedding failure.
    - anything else (incl. the default ``"lexical"``) → :class:`LexicalToolSelector`.
    """
    if settings.tool_selection == "all":
        return AllToolsSelector(registry)
    fallback = LexicalToolSelector(
        registry,
        budget=settings.tool_budget,
        core_extra=frozenset(settings.tool_core_extra),
        core_remove=frozenset(settings.tool_core_remove),
    )
    if settings.tool_selection == "embedding":
        return EmbeddingToolSelector(
            registry,
            embedder=_ollama_embedder(settings),
            fallback=fallback,
            budget=settings.tool_budget,
            core_extra=frozenset(settings.tool_core_extra),
            core_remove=frozenset(settings.tool_core_remove),
        )
    return fallback
```

(`_ollama_embedder` is constructed eagerly here, but it only *constructs* a `Client` — it does not contact Ollama until `embed` is first called on a real turn. Tests that build the embedding selector therefore touch no network; the embedding behavior itself is covered by Task 3 with injected fakes.)

- [ ] **Step 4: Run tests + mypy + import smoke**

Run: `uv run pytest tests/unit/test_tool_selection.py -v` → PASS (all, incl. the 2 new).
Run: `uv run python -c "from autobot.tools.selection import build_tool_selector; print('ok')"` → prints `ok`.
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Full gate**

Run: `make check`
Expected: PASS (ruff + ruff-format + mypy strict + full pytest suite all green). No `app.py` change is needed — `_build_llm` already calls `build_tool_selector(settings, registry)`, so `tool_selection="embedding"` is now honored end-to-end.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): route tool_selection=embedding to EmbeddingToolSelector"
```

---

### Task 5 (OPTIONAL): Conservative MCP `inputSchema` minification

> **Status: OPTIONAL — included as a clearly-marked, self-contained final task, not a hard requirement.** Justification: it is a *pure*, near-lossless token win (~25–40% on verbose Slack/GitHub schemas) that is fully unit-testable with no model, and it is gated to MCP-tool ingestion only — so it carries low risk and clear value. But it is orthogonal to the embedding selector (the headline of Phase 4) and touches the live `_sync_tools` path, so it ships **last**, behind its own commit, and can be dropped without affecting Tasks 1–4. The minifier is deliberately **conservative**: it strips whitespace and drops only *verbose nested `description` strings*, and **never** touches `type`, `required`, `enum`, `properties` keys, or any value the model needs to call the tool correctly. If you judge even this too risky at review time, skip Task 5 — Phase 4's required scope (the embedding selector) is complete after Task 4.

**Files:**
- Create: `src/autobot/tools/schema_min.py`
- Modify: `src/autobot/mcp/session.py`
- Test: `tests/unit/test_schema_min.py` (create)

**Interfaces:**
- Produces: `minify_schema(schema: dict[str, Any]) -> dict[str, Any]` — a pure, deep-copy transform: recursively drop `description` keys nested **below** the top level of the schema (keep the tool's own top-level `description`, which the model relies on), and collapse runs of whitespace inside any remaining string values. Preserves `type`, `required`, `enum`, `properties`, `items`, `default`, and every structural key.
- Consumes (session): `minify_schema` applied to `adapter.params_from_input_schema(tool.inputSchema)` in `_sync_tools` before it becomes `ToolSpec.parameters`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_schema_min.py`:

```python
"""Tests for the pure, conservative MCP input-schema minifier (no model, no network)."""

from __future__ import annotations

from autobot.tools.schema_min import minify_schema

_VERBOSE = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "description": "The channel    ID   to post to.\n\n  Use the public ID.",
            "enum": ["general", "random"],
        },
        "count": {"type": "integer", "default": 1, "required": True},
    },
    "required": ["channel"],
}


def test_preserves_type_required_enum_and_structure() -> None:
    out = minify_schema(_VERBOSE)
    assert out["type"] == "object"
    assert out["required"] == ["channel"]
    assert out["properties"]["channel"]["type"] == "string"
    assert out["properties"]["channel"]["enum"] == ["general", "random"]
    assert out["properties"]["count"]["type"] == "integer"
    assert out["properties"]["count"]["default"] == 1
    assert out["properties"]["count"]["required"] is True


def test_drops_nested_descriptions_only() -> None:
    out = minify_schema(_VERBOSE)
    assert "description" not in out["properties"]["channel"]  # nested → dropped


def test_keeps_top_level_description() -> None:
    out = minify_schema({"type": "object", "description": "Top  level.", "properties": {}})
    assert out["description"] == "Top level."  # top-level kept (whitespace collapsed)


def test_is_pure_does_not_mutate_input() -> None:
    import copy

    original = copy.deepcopy(_VERBOSE)
    minify_schema(_VERBOSE)
    assert _VERBOSE == original  # input untouched (deep copy inside)


def test_shrinks_serialized_size() -> None:
    import json

    before = len(json.dumps(_VERBOSE))
    after = len(json.dumps(minify_schema(_VERBOSE)))
    assert after < before  # net token win
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_schema_min.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.tools.schema_min`.

- [ ] **Step 3: Write the minifier**

Create `src/autobot/tools/schema_min.py`:

```python
"""Pure, conservative minification of an MCP tool's JSON-Schema parameters.

Verbose server schemas (Slack/GitHub) carry long nested ``description`` strings and
loose whitespace that cost tokens on every advertised round without helping a local
model call the tool. This trims them **near-losslessly**: it collapses whitespace and
drops ``description`` keys nested *below* the schema's top level, but never touches
``type``, ``required``, ``enum``, ``properties``, ``items``, ``default`` or any other
value the model needs. The tool's own top-level description is preserved (the model
relies on it to choose the tool). Pure and synchronous — unit-tested without a runtime.
"""

from __future__ import annotations

import re
from typing import Any

_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return _WS.sub(" ", text).strip()


def _walk(node: Any, *, top: bool) -> Any:
    """Recursively copy ``node``, collapsing strings and dropping nested descriptions."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            # Drop only *nested* descriptions; keep the schema's top-level one.
            if key == "description" and not top:
                continue
            out[key] = _walk(value, top=False)
        return out
    if isinstance(node, list):
        return [_walk(item, top=False) for item in node]
    if isinstance(node, str):
        return _collapse(node)
    return node


def minify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a token-trimmed copy of an MCP parameters schema (conservative).

    Args:
        schema: A JSON-Schema object (a tool's ``parameters``).

    Returns:
        A new dict (the input is never mutated): whitespace collapsed in all string
        values; ``description`` keys below the top level removed. Structural and
        call-critical keys (``type``/``required``/``enum``/``properties``/…) are kept
        verbatim, so the advertised signature stays valid.
    """
    result = _walk(schema, top=True)
    assert isinstance(result, dict)  # top-level schema is always an object
    return result
```

- [ ] **Step 4: Run minifier tests + mypy**

Run: `uv run pytest tests/unit/test_schema_min.py -v` → PASS (5).
Run: `uv run mypy` → `Success`.

- [ ] **Step 5: Apply it at the MCP ingestion seam**

In `src/autobot/mcp/session.py`, in `_sync_tools`, the `ToolSpec` construction sets `parameters=adapter.params_from_input_schema(tool.inputSchema)`. Wrap it with the minifier:

```python
            desired[reg_name] = ToolSpec(
                name=reg_name,
                description=tool.description or "",
                parameters=minify_schema(adapter.params_from_input_schema(tool.inputSchema)),
                handler=self._make_handler(tool.name),
                risk=adapter.risk_for(tool, floor=floor, overrides=overrides),
                network=network,
            )
```

Add the import near the other intra-package imports at the top of `session.py`:

```python
from autobot.tools.schema_min import minify_schema
```

(`schema_min` is pure stdlib and import-light, so this does not weigh down `session.py`'s import path.)

- [ ] **Step 6: Run the MCP session tests + format + mypy**

Run: `uv run pytest tests/unit/test_mcp_session.py -v` → PASS (minification is near-lossless; existing sync/fingerprint tests still hold — the fingerprint is computed from the raw `tool` before minification, so re-consent behavior is unchanged).
Run: `uv run ruff format .` then `uv run mypy` → `Success`.

- [ ] **Step 7: Full gate**

Run: `make check`
Expected: PASS (ruff + ruff-format + mypy strict + full pytest suite all green).

- [ ] **Step 8: Commit**

```bash
git add src/autobot/tools/schema_min.py src/autobot/mcp/session.py tests/unit/test_schema_min.py
git commit -m "perf(mcp): minify MCP input schemas (conservative, near-lossless) to cut tokens"
```

---

## Manual smoke-test (after Task 4; Task 5 optional)

With a live Ollama and the embedding model pulled (`ollama pull nomic-embed-text`):

1. Set `"tool_selection": "embedding"` in `~/.autobot/settings.json`, `make run`, then ask **"hi"** — confirm a normal reply and that the context line is the same small baseline as lexical (core only; no embedder cost on an empty/irrelevant gated match beyond one cheap query embedding).
2. Ask a **paraphrased** intent that lexical might miss (e.g. "drop a note to the team channel" for a Slack send tool) with an MCP server connected — confirm the embedding selector surfaces the right gated tool where lexical would have needed `find_tools`.
3. **Force a fallback:** rename the model to a non-existent one (`"embedding_model": "does-not-exist"`), restart, and confirm Jack still answers (lexical fallback) and `~/.autobot/logs/autobot.log` shows a single `[tools] embedding selection failed; falling back to lexical` warning (`make logs-grep C=tools`).
4. (If Task 5 shipped) Connect a verbose MCP server (Slack/GitHub) and confirm tools still call correctly — the minified schemas keep type/required/enum, so argument validation is unaffected — while the per-tool schema is visibly smaller.

---

## Self-Review

**1. Spec coverage** (design §11 P4: "`EmbeddingToolSelector` (local path); MCP `inputSchema` minification for selected tools (~25–40% extra, near-lossless)", grounded in §7):
- `embedding_model` setting (download-on-first-use, local-only) → Task 1 ✓ (design §7, §8).
- Pure embedding primitives (`cosine`, `embed_doc`, `_doc_key` fingerprint-equivalent cache key) → Task 2 ✓ (design §7 "cache vectors keyed by fingerprint; unchanged → never re-embedded" — keyed on a SHA-256 of the embedded doc, justified in Task 2 because the registry holds `ToolSpec`s, not MCP `Tool`s).
- `EmbeddingToolSelector` implementing the **full** protocol (`select` + Phase-2 `search`), same core/budget/pinned rule, cosine-ranked gated, tool-side embed-once cache, query-once-per-call, REQUIRED lexical fallback on any embedding error (warned once) → Task 3 ✓ (design §7; risk-table "lexical recall miss → EmbeddingToolSelector upgrade path"; §10 graceful fallback).
- `build_tool_selector` routes `"embedding"` → `EmbeddingToolSelector` with the real local Ollama embedder + lexical fallback; `"all"`/`"lexical"` unchanged → Task 4 ✓ (design §8 `tool_selection` values; §4b "Protocol keeps a future EmbeddingToolSelector a one-line build() swap").
- Cloud path untouched: `EmbeddingToolSelector` is only reachable via `tool_selection="embedding"` on the Ollama path; the Anthropic path keeps its native Tool Search (design §6, §7 "Cloud path does not need it") — no `app.py`/`anthropic_llm.py` change in Phase 4.
- Optional conservative `inputSchema` minification (~25–40%, near-lossless: keeps type/required/enum, drops only nested descriptions + whitespace), applied at the `_sync_tools` ingestion seam → Task 5 ✓, clearly marked OPTIONAL with a justification and droppable without touching Tasks 1–4 (design §11 P4 last bullet; §6 caching note that count-reduction, not lossless trims alone, is the lever).
- 100% on-device: only tool docs + the query are embedded, via the local Ollama endpoint; no new base dependency (model pulled on first use, opt-in) → satisfied across Tasks 1, 3, 4.

**2. Placeholder scan:** none — every code step shows complete, runnable code; every run step states the command and the expected result. The one "find the construction" step (Task 5, Step 5) quotes the exact `ToolSpec(...)` block from `_sync_tools` with the before/after, and mypy is the safety net. The Task-4 test note explicitly resolves the empty-query/live-Ollama ambiguity by asserting construction + type only (embedding behavior is covered by Task 3 with injected fakes).

**3. Type consistency:**
- `Embedder = Callable[[str], list[float]]` — defined Task 2; the injected param in `EmbeddingToolSelector.__init__` (Task 3), the return of `_ollama_embedder` (Task 4), and the test fakes all match `Callable[[str], list[float]]`.
- `cosine(a: Sequence[float], b: Sequence[float]) -> float`, `embed_doc(spec: ToolSpec) -> str`, `_doc_key(spec: ToolSpec) -> str` — consistent between Task 2 definitions and Task 3 usage.
- `EmbeddingToolSelector(registry, *, embedder, fallback, budget, core_extra: frozenset[str], core_remove: frozenset[str])` with `select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]` and `search(self, intent: str, *, limit: int = 5) -> list[str]` — matches the `ToolSelector` protocol (`select` from Phase 1, `search` from Phase 2) exactly; consistent between Task 3 (definition) and Task 4 (`build_tool_selector` call site).
- `build_tool_selector(settings: Settings, registry: ToolRegistry) -> ToolSelector` — signature unchanged from Phase 1; consistent with the existing `app._build_llm` call site (no app change needed).
- `minify_schema(schema: dict[str, Any]) -> dict[str, Any]` — consistent between Task 5 definition and the `_sync_tools` call site (wrapping `adapter.params_from_input_schema(...)`, which already returns `dict[str, Any]`).
- Ollama embedding call: `Client.embed(model: str, input: str) -> EmbedResponse` with `.embeddings: list[list[float]]` — verified against the installed package (`embed` / `embeddings` both present; `embed` chosen); `resp.embeddings[0]` is the single query/doc vector.

No issues found.
