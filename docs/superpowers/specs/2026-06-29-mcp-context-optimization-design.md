# MCP Context Optimization — Relevance-Gated Tool Advertising (Design)

**Date:** 2026-06-29
**Branch:** `feat/mcp-integration`
**Status:** Approved design — pending implementation plan.

> **What this is.** A design to stop Jack from advertising every registered tool on
> every turn. Today context grows **linearly with tool count**; connecting MCP servers
> bloats every request — hurting cost, latency, **and** (on a small local model) tool-
> selection accuracy. This replaces "advertise everything" with **relevance-gated
> advertising** bounded to a per-turn budget, 100% on-device, with a model-driven
> escape hatch so a missed pre-filter self-heals.

---

## 1. Problem statement (confirmed in code)

Every LLM request funnels its tool list through `ToolRegistry.schemas()`
([registry.py:106-110](../../../src/autobot/tools/registry.py#L106-L110)), which returns
**every registered tool with no filtering**, and it is called on **every** turn:

- Ollama path: [ollama_llm.py:292](../../../src/autobot/llm/ollama_llm.py#L292) (inside
  `_chat()`, so it re-sends on every tool-result round too).
- Anthropic path: [anthropic_llm.py:540](../../../src/autobot/llm/anthropic_llm.py#L540).

MCP tools land in the **same global registry**
([session.py `_sync_tools`](../../../src/autobot/mcp/session.py#L531-L627)) with their raw
`inputSchema` copied verbatim
([adapter.py `params_from_input_schema`](../../../src/autobot/mcp/adapter.py#L59-L67)).
Slack/GitHub schemas are large, so each connected server adds thousands of tokens to
**every** request, forever.

There is **no** tool selection, relevance gating, schema budgeting, or schema trimming
anywhere — only conversation-history compaction (which is orthogonal).

**Observed symptoms:** "hi" ≈ 3k tokens (system prompt + ~68 always-on built-in schemas);
"what's my battery?" with MCP connected ≈ 26k tokens (all that, plus every MCP server's
tool schemas).

**Why this matters beyond cost.** The default model is `qwen3:8b`. Small models degrade
sharply as the tool set grows:

- *Less is More* ([arXiv 2411.15399](https://arxiv.org/abs/2411.15399)): **Llama-3.1-8B
  fails to select the correct tool when given 46 tools, but succeeds with 19**; tool
  reduction raised its selection accuracy to **93.8%**.
- OpenAI function-calling guidance: keep **"fewer than 20 functions"** available per turn.
- Cursor caps at **40 tools** explicitly to avoid "flooding the agent's context window."
- *RAG-MCP* ([arXiv 2505.03275](https://arxiv.org/abs/2505.03275)): retrieving the relevant
  subset instead of dumping all tools **tripled** selection accuracy (13.6% → 43.1%) and
  cut prompt tokens **>50%**.

So bounding per-turn tools to ~20 is simultaneously a cost fix **and** the single biggest
accuracy lever we have on the local model.

---

## 2. Goals, non-goals, success criteria

**Goals**
- Per-turn tool context bounded to a small budget (~20 tools) regardless of how many tools
  are registered.
- Connecting an MCP server adds **~0 baseline tokens** — cost appears only when one of its
  tools is actually relevant.
- A missed pre-filter is recoverable **within the same turn** (no dead-ends).
- 100% on-device, no new runtime dependency for the baseline path, privacy preserved.
- The existing gate / audit / sandbox / dispatch path is untouched.

**Non-goals (YAGNI)**
- Code execution / sandboxed model-generated code (Anthropic's 98.7% approach) — fights
  the permission-gate and privacy model. Future, not now.
- Embeddings on the baseline path — lexical first; embeddings are an optional local-path
  upgrade (§7).
- Changing conversation-history compaction (already exists, orthogonal).

**Success criteria**

| Query | Today | Target |
|---|---|---|
| "hi" | ~3k | ~1–1.5k (system prompt + core set only) |
| "what's my battery?" (10 MCP servers connected) | ~26k | ~1.5k (`battery_status` is core; MCP ≈0) |
| Adding the 11th MCP server | +Nk baseline / turn | **+0 baseline** |
| Tools the model chooses among | 100+ | ~15–20 |

---

## 3. How comparable products solve it (research summary)

Three converging strategies — all "don't show the model tools it doesn't need":

| Strategy | Who ships it | Measured result | Fit for us |
|---|---|---|---|
| **Deferred loading + tool-search** (model discovers tools on demand; deferred defs cost ~0 until searched) | Anthropic **Tool Search Tool** (`defer_loading: true`); on by default in Claude Code 2.1.x; OpenAI MCP `defer_loading` | ~85% token cut; MCP-eval accuracy 49→74% (Opus 4), 79.5→88.1% (Opus 4.5); ([Anthropic](https://www.anthropic.com/engineering/advanced-tool-use)) | **Cloud path** uses this natively; we mirror it client-side via `find_tools` |
| **Semantic / lexical pre-retrieval** (client picks the subset *before* the LLM call) | **RAG-MCP**, langgraph-bigtool, LlamaIndex `ObjectRetriever`, Cursor/Windsurf proxies | >50% fewer tokens, accuracy 13.6→43.1% ([arXiv 2505.03275](https://arxiv.org/abs/2505.03275)) | **Local path** baseline |
| **Code execution** (tools as filesystem API, results stay in sandbox) | Anthropic ["Code execution with MCP"](https://www.anthropic.com/engineering/code-execution-with-mcp) | 150k→2k tokens, 98.7% | **Out of scope** (sandbox/security cost) |
| Hard caps + manual toggles (fallback) | Cursor (40), Windsurf (100), OpenAI (128 hard) | blunt; pushes work to user | complementary, not primary |

**Key caveat the research flags (ToolRet, [arXiv 2503.01763](https://arxiv.org/abs/2503.01763)):**
retrieval **recall** becomes the new bottleneck — a retrieval miss means the model simply
*can't* call the right tool. Our `find_tools` escape hatch (§4d) is the direct mitigation.

**Caching is necessary but not sufficient** ([Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)):
it makes carrying tool defs ~90% cheaper but is *lossless* — the model still sees and
attends to every tool, so it does **not** fix the accuracy-from-too-many-tools problem.
We use caching on the cloud path as a bonus (§6), but reducing the **count** is the lever
that helps both cost and accuracy.

---

## 4. Design

### Overview — one seam

Introduce a **`ToolSelector`** component (swappable, wired in `app.py::build()` like the
STT/LLM engines). The LLM clients call the selector **instead of** `registry.schemas()`.
The registry stays a passive store; all selection logic lives in one pure, unit-testable
module. No orchestrator change.

```
build() ─┬─ ToolRegistry (unchanged; passive catalog)
         ├─ ToolSelector  (NEW; reads the registry, returns a bounded subset)
         └─ LLM client ── uses selector.select(query, pinned) instead of registry.schemas()
```

### (a) Tool tiering

Add one field to the frozen `ToolSpec` (same additive pattern as `network`):

```python
core: bool = False   # advertised on EVERY turn; the small always-on set
```

- ~12 frequent built-ins are marked `core=True` next to their definitions (the repo's
  "per-tool metadata lives next to the tool" convention): e.g. `get_time`,
  `battery_status`, `set_volume`, `set_brightness`, wifi status, open/focus app,
  `read_clipboard`, create-reminder. (Exact set finalized in the plan.)
- **MCP tools are never `core`.** The adapter never sets it, so connecting servers adds
  **0 baseline tokens**.
- A `settings.json` override (`tool_core_extra` / `tool_core_remove`, names) lets the core
  set be re-tuned without code edits.

`find_tools` (§4d) is always advertised too, independent of the budget.

### (b) `ToolSelector` — protocol + lexical default

A `Protocol` in `core/interfaces.py`; the concrete `LexicalToolSelector` in a new
`tools/selection.py` (pure logic → unit-tested without a model/mic, matching the repo rule).

```python
# core/interfaces.py
class ToolSelector(Protocol):
    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Bounded set to advertise this round: core ∪ find_tools ∪ pinned ∪ top-K(query)."""
    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Names of the best gated tools for an explicit find_tools intent."""
```

`LexicalToolSelector` (constructed with the registry + budget):
- Scores each **gated** tool with BM25/keyword matching over its **`name + description`**
  against the current user message (+ the last user turn). Jack's descriptions already
  pack synonyms (per `CLAUDE.md`), which is exactly the signal lexical matching needs —
  so recall starts strong.
- `select()` returns `core ∪ {find_tools} ∪ resolve(pinned) ∪ top-K gated`, deduped and
  capped at `tool_budget`. K = `budget − |core advertised| − |pinned|`.
- Pure and synchronous; ~sub-millisecond for hundreds of tools.

The Protocol keeps a future `EmbeddingToolSelector` (§7) a one-line `build()` swap.

### (c) Per-turn advertised set + budget

```
advertised = core(always) + top-K gated-by-relevance + pinned(from find_tools) + find_tools
           capped at `tool_budget`  (default ~20, tunable in settings.json)
```

Core ≈12 leaves ~7 slots for retrieved tools on a fresh turn; pinned tools added by
`find_tools` extend the set for the remainder of the turn.

### (d) `find_tools` escape hatch (progressive disclosure, client-side)

A built-in meta-tool, **always advertised**:

```
find_tools(intent: str) -> str
```

When the lexical pre-filter misses, the model calls `find_tools("send a slack message")`.
The **turn loop** detects the call, runs `selector.search(intent)`, **pins** the matched
tool names for the rest of the turn, and returns a short summary of the matched tools to
the model. On the next round, `select(query, pinned=…)` includes those pinned tools, and
the model uses them. Self-healing within the same turn — the direct answer to the recall
bottleneck.

State note: the per-turn `pinned: set[str]` lives in the LLM client's turn loop (reset at
the start of each user turn); the loop owns both the search-and-pin and the normal dispatch
of `find_tools`. (Mechanism refined in the plan.)

---

## 5. Per-turn data flow (Ollama / local path)

1. User message arrives; turn loop resets `pinned = {}`.
2. `selector.select(user_text, pinned={})` → `core ∪ top-K ∪ {find_tools}`.
3. `_chat()` sends only those schemas.
4. Model calls a **real tool** → executes through the existing gate / audit / sandbox,
   **unchanged**.
5. Model calls **`find_tools(intent)`** → loop runs `selector.search(intent)`, adds matches
   to `pinned`, returns the match summary; next `_chat()` uses
   `select(user_text, pinned)` = `core ∪ top-K ∪ pinned ∪ {find_tools}`.
6. Loop ends when the model produces a final answer (unchanged termination).

---

## 6. Anthropic cloud path (bonus, near-free)

On the Anthropic provider, delegate discovery to the **native Tool Search Tool** instead of
the client-side shim:

- Advertise `core` tools normally; mark **gated** tools `defer_loading: true`; add the
  `tool_search_tool` (`tool_search_tool_bm25_20251119`).
- Same tiering metadata drives both paths; discovery + caching happen server-side
  (prompt-cache-preserving).
- Enable **prompt caching** (`cache_control` on the last tool of the now-small, stable
  prefix) — lossless cost/latency win.
- **Gated on model support**, with graceful fallback to the `LexicalToolSelector` when the
  configured Anthropic model doesn't support tool-search.

This path needs no embeddings and no `find_tools` shim — Anthropic supplies the equivalent.

---

## 7. Optional: embeddings (local path only)

An `EmbeddingToolSelector` is a drop-in upgrade for **recall on the local path** when
lexical isn't finding paraphrased intents. Cadence and scope (decided during design):

- **Tool-side index: one-time + on-change, amortized to ~0.** Embed each tool's
  `name + description` once on registration; cache vectors keyed by `adapter.fingerprint(tool)`
  ([adapter.py](../../../src/autobot/mcp/adapter.py)); unchanged fingerprint → never
  re-embedded.
- **Query-side: once per turn** — embed the user message (~10–50ms locally) + cosine search
  over cached vectors (microseconds).
- **Runs locally always** (on-device), even when the LLM is cloud — only tool descriptions
  and the query are embedded, neither leaves the Mac. One-time model download
  (`nomic-embed-text` via Ollama, ~270MB) fitting the "download on first use" pattern.
- **Cloud path does not need it** — native Tool Search handles discovery server-side.
  (Optionally, the local embedding index *could* feed Anthropic's custom client-side search
  for better-than-BM25 recall, but that's a refinement, not required.)

Embeddings are therefore strictly a local-path enhancement; lexical (P1) is the universal
baseline on both paths.

---

## 8. Config additions (`settings.json`, via `config.py`)

| Field | Default | Meaning |
|---|---|---|
| `tool_budget` | `20` | Max tools advertised per round (excl. always-on `find_tools`). |
| `tool_selection` | `"lexical"` | `"lexical"` \| `"embedding"` \| `"all"` (escape hatch to disable gating). |
| `tool_core_extra` | `[]` | Tool names to force into the core set. |
| `tool_core_remove` | `[]` | Core tool names to demote to gated. |

`"all"` reproduces today's behavior for debugging/comparison. No secrets involved.

---

## 9. Testing strategy (pure logic, no model runtime)

- **Selector scoring:** query → expected top-K ordering over a fixed fake catalog.
- **Tiering:** core tools always present; MCP tools never appear in the baseline set;
  `find_tools` always present.
- **Budget:** never exceeds `tool_budget`; core + pinned prioritized over retrieved.
- **`find_tools` pin-and-expand:** simulate a `find_tools` call, assert pinned tools appear
  on the next round and persist for the rest of the turn, then reset next turn.
- **Settings:** `tool_core_extra`/`remove` and `tool_selection="all"` behave as specified.
- **Regression:** dispatch / gate / audit / sandbox untouched; existing tool tests pass.
- **Anthropic path:** gated tools carry `defer_loading`; tool-search tool present; fallback
  to lexical when model unsupported (unit-level with a fake).

---

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Lexical recall miss on paraphrased intent | `find_tools` escape hatch (self-heals in-turn) + synonym-rich descriptions + `EmbeddingToolSelector` upgrade path |
| Small model doesn't call `find_tools` | Core set covers the common cases so it's rarely needed; strong `find_tools` description; general system-prompt principle about discovering tools; `tool_budget` tunable upward |
| Multi-intent query needs several tool families | Score the full message; K large enough; `find_tools` covers gaps |
| Cloud tool-search unsupported by chosen model | Graceful fallback to lexical selector |
| Pinned-state leakage across turns | `pinned` reset at the start of every user turn (owned by the turn loop) |

---

## 11. Phasing

- **P1 — Kill the bloat.** `ToolSpec.core` + tiering, `LexicalToolSelector`, `tool_budget`,
  config fields, wired into the Ollama client (replace `registry.schemas()` with
  `selector.select(...)`). Mark the core built-ins; MCP stays gated.
- **P2 — Make it safe.** `find_tools` meta-tool + per-turn pinning in the turn loop.
- **P3 — Cloud parity.** Anthropic native Tool Search (`defer_loading` + tool-search tool)
  + prompt caching, with lexical fallback.
- **P4 — Optional.** `EmbeddingToolSelector` (local path); MCP `inputSchema` minification
  for selected tools (~25–40% extra, near-lossless).

Each phase is independently shippable and independently improves the numbers.

---

## 12. Sources

- Anthropic — Advanced tool use / Tool Search Tool: https://www.anthropic.com/engineering/advanced-tool-use
- Anthropic — Code execution with MCP: https://www.anthropic.com/engineering/code-execution-with-mcp
- Anthropic — Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- RAG-MCP (prompt bloat in tool selection): https://arxiv.org/abs/2505.03275
- Less is More (function calling on edge devices; 8B 46→fail/19→ok): https://arxiv.org/abs/2411.15399
- ToolRet (retrieval recall is the bottleneck): https://arxiv.org/abs/2503.01763
- langgraph-bigtool (retrieve_tools pattern): https://github.com/langchain-ai/langgraph-bigtool
- Cursor 40-tool limit: https://forum.cursor.com/t/tools-limited-to-40-total/67976
- OpenAI function-calling guidance ("fewer than 20 functions"): https://developers.openai.com/api/docs/guides/function-calling
