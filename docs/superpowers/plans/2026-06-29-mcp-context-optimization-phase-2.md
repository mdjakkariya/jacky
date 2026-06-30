# MCP Context Optimization — Phase 2: `find_tools` Escape Hatch + Per-Turn Pinning (local/Ollama)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make relevance gating *safe*. Phase 1 bounds the per-turn tool set, but a lexical pre-filter can miss a paraphrased intent — and a missed tool is a dead end (the model literally can't call it). Add a model-driven escape hatch: a `find_tools(intent)` meta-tool, **always advertised**, that the model calls when the pre-filtered set lacks what it needs. The turn loop detects the call, runs `selector.search(intent)`, **pins** the matched tool names for the rest of the turn, and hands the model a short summary so it can call the real tool on the next round. Self-healing within the same turn — the direct answer to the retrieval-recall bottleneck (design §3, §4d, §10).

**Architecture:** Two seams, both already prepared by Phase 1. (1) The `ToolSelector` protocol gains a second method, `search(intent, *, limit) -> list[str]`, implemented on both existing selectors using the Phase-1 pure scorer (`score_tools`); it returns bare tool *names* of the best **gated** matches. (2) `OllamaLanguageModel` grows a per-turn `self._pinned: set[str]` (reset each `run_turn`); the turn loop intercepts `find_tools` calls (it never goes through the registry/gate — the loop owns its semantics), fills `self._pinned` via `selector.search`, and `_tools_for_round()` now passes `pinned=frozenset(self._pinned)` into `select(...)` **and** always appends the `find_tools` schema. `find_tools` is a module-level `ToolSpec` constant in `tools/builtin.py` (single source for its name + description + schema) that is **not** registered in the registry — so it never competes for a budget slot, never reaches `dispatch`, and is impossible to double-advertise. No orchestrator change. Behavior is backward-compatible: the no-selector legacy path is untouched (it advertises every tool, so an escape hatch is both meaningless and unnecessary there — justified in Task 3).

**Tech Stack:** Python 3.11, dataclasses, stdlib only (no new deps), pytest, mypy strict, ruff.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` in every module.
- mypy **strict** over `src` AND `tests` — keep it green. Run `uv run mypy`.
- Google-style docstrings on every public module/class/function (ruff `D`); **tests exempt**.
- Line length 100; never hand-format — run `uv run ruff format .` (or `make format`).
- Value objects are `frozen=True, slots=True` dataclasses; no business logic on them.
- **No new runtime dependency** — discovery reuses the Phase-1 stdlib scorer and is 100% on-device.
- **Conventional Commits, NO `Co-Authored-By` / AI-attribution trailer.** Stage explicit paths only — never `git add -A`/`.`/`-u`.
- Verification gate per task: `make check` green (ruff + ruff-format + mypy + pytest). Run a single file with `uv run pytest tests/unit/<file>.py -v`.
- **Branch:** continue on `feat/mcp-integration`. All Phase-2 commits stack there.

**Interfaces already on the branch (Phase 1 — consume, do not rebuild):**
- `autobot.tools.registry.ToolSpec` — `frozen=True, slots=True`: `name, description, parameters, handler, risk=Risk.READ_ONLY, confirm_prompt=None, ack=None, requires=None, network=False, core=False`; `to_schema() -> dict`.
- `autobot.tools.registry.ToolRegistry` — `register(spec, *, replace=False)`, `unregister(name)->bool`, `get(name)->ToolSpec|None`, `schemas()->list[dict]`, `specs()->list[ToolSpec]`, `dispatch(name, arguments)->ToolResult`.
- `autobot.core.interfaces.ToolSelector` Protocol — currently `select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]`.
- `autobot.tools.selection` — `tokenize(text)->list[str]`; `score_tools(query, specs)->list[tuple[ToolSpec,float]]` (relevance-desc, then name-asc; zero-score excluded; empty/usable-less query → `[]`); `AllToolsSelector(registry)`; `LexicalToolSelector(registry, *, budget, core_extra: frozenset[str], core_remove: frozenset[str])`; `build_tool_selector(settings, registry)->ToolSelector`.
- `autobot.tools.builtin` — `get_time()`/`GET_TIME` (a `core=True` `ToolSpec`); `register_builtins(registry)`.
- `autobot.llm.ollama_llm.OllamaLanguageModel(settings, registry, transcript=None, memory=None, client=None, selector=None)` — `self._selector`, `self._round_query`; `_tools_for_round()` returns `[s.to_schema() for s in selector.select(self._round_query)]` (or `registry.schemas()` when `selector is None`); `_chat()` sets `kwargs["tools"] = self._tools_for_round()`; `run_turn(user_text, execute)` runs the bounded tool loop, setting `self._round_query = user_text`.
- `autobot.core.types` — `ToolCall(name, arguments={})`, `ToolResult(name, content, ok=True)`, `Risk` (READ_ONLY/WRITE/DESTRUCTIVE), `ToolExecutor = Callable[[ToolCall], ToolResult]`.
- Test helpers in `tests/unit/test_ollama_llm.py`: `_FakeOllama` (records `self.calls` = list of `chat(**kwargs)` dicts, so `calls[i]["tools"]` is the advertised schema list), `_resp(content="", tool_calls=None)`, `_tc(name, args)`, `_registry()`. `_FakeOllama.show()` returns `{}` (so `_resolve_context` is safe even without `context_tokens`).

## File Structure

| File | Responsibility |
|---|---|
| `src/autobot/core/interfaces.py` (modify) | Add `ToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` to the Protocol |
| `src/autobot/tools/selection.py` (modify) | Implement `search(...)` on `AllToolsSelector` and `LexicalToolSelector` |
| `src/autobot/tools/builtin.py` (modify) | Add the `FIND_TOOLS` `ToolSpec` constant + `find_tools` handler (the schema source; not registered) |
| `src/autobot/llm/ollama_llm.py` (modify) | Per-turn `self._pinned`; intercept `find_tools` in the loop; `_tools_for_round` passes `pinned` + always appends the `find_tools` schema |
| `tests/unit/test_tool_selection.py` (modify) | `search()` tests for both selectors |
| `tests/unit/test_ollama_llm.py` (modify) | `find_tools` pins + appears next round; pins reset between turns; `find_tools` always advertised |

---

### Task 1: Extend the `ToolSelector` protocol with `search`

**Files:**
- Modify: `src/autobot/core/interfaces.py`
- Test: (none — a Protocol method has no runtime behavior to assert; coverage lands in Task 2.)

**Interfaces:**
- Consumes: `ToolSpec` (already imported under `TYPE_CHECKING`).
- Produces: `ToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` added to the existing Protocol. **This signature is the cross-phase contract — Phase 4's `EmbeddingToolSelector` will implement both `select` and this `search`. Keep it stable.**

- [ ] **Step 1: Add the `search` method to the `ToolSelector` Protocol**

In `src/autobot/core/interfaces.py`, the `ToolSelector` Protocol currently ends after the `select` method (the last method in the file). Add the `search` method immediately after `select`'s body (inside the same class):

```python
    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Return the names of the best gated tools for an explicit intent.

        The model's escape hatch: when the relevance-gated set advertised by
        :meth:`select` lacks the tool a request needs, the model calls
        ``find_tools(intent)`` and the turn loop forwards ``intent`` here. The
        returned names are then pinned (force-advertised via ``select(..., pinned)``)
        for the rest of the turn, so the model can call the real tool next round.

        Args:
            intent: A short natural-language description of what the model wants to
                do (e.g. ``"send a message on slack"``).
            limit: Maximum number of tool names to return.

        Returns:
            Up to ``limit`` bare tool names, most relevant first. Never includes
            always-on core tools (the model already sees those). Empty when nothing
            matches.
        """
        ...
```

(`@runtime_checkable` already decorates the Protocol — adding a method keeps the same decorator. The `# Imported only for type checking` block already imports `ToolSpec`; `list[str]` needs no new import.)

- [ ] **Step 2: Verify the protocol still imports + mypy is green**

Run: `uv run python -c "from autobot.core.interfaces import ToolSelector; print('ok')"` → prints `ok`.
Run: `uv run mypy` → `Success: no issues found`.

Note: mypy will now flag any `ToolSelector` implementer that lacks `search` — but `AllToolsSelector`/`LexicalToolSelector` are *structural* (duck-typed) implementers, only checked against the Protocol where one is assigned to a `ToolSelector`-typed binding. The single such site is `build_tool_selector(...) -> ToolSelector` (return type) in `selection.py`; Task 2 adds `search` to both classes in the same module, so run mypy again at the end of Task 2 — it stays green across the pair. If you run mypy *between* Tasks 1 and 2 and see "incompatible return type" on `build_tool_selector`, that is expected and resolved by Task 2; the standalone `python -c` import above is the gate for this task.

- [ ] **Step 3: Commit**

```bash
git add src/autobot/core/interfaces.py
git commit -m "feat(tools): add ToolSelector.search for the find_tools escape hatch"
```

---

### Task 2: Implement `search` on `AllToolsSelector` and `LexicalToolSelector`

**Files:**
- Modify: `src/autobot/tools/selection.py`
- Test: `tests/unit/test_tool_selection.py`

**Interfaces:**
- Consumes: `score_tools`, `ToolRegistry.specs()`, `ToolSpec.core`.
- Produces:
  - `AllToolsSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` — top `limit` names by `score_tools` over **all** registered specs (the "all" mode has no gated/core distinction, so it ranks everything).
  - `LexicalToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` — top `limit` names by `score_tools` over **gated** specs only (core names — `{s.name for s in specs if s.core} | core_extra − core_remove` — are excluded, since the model already sees core every round).

**Search rule (both):** rank with the Phase-1 scorer (relevance-desc, name-asc, zero-score excluded), then take the first `limit` names. An intent with no usable terms → `[]` (inherited from `score_tools`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tool_selection.py` (`AllToolsSelector`, `LexicalToolSelector`, `ToolRegistry`, `_spec`, `_reg`, `_lexical` are all already defined/imported in that file):

```python
# Phase 2 tests: ToolSelector.search


def test_all_tools_search_ranks_by_relevance() -> None:
    reg = ToolRegistry()
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    reg.register(_spec("github__issue", "Create a GitHub issue."))
    names = AllToolsSelector(reg).search("send a slack message")
    assert names[0] == "slack__send"
    assert "github__issue" not in names  # scored 0 → excluded by score_tools


def test_lexical_search_returns_gated_names_excluding_core() -> None:
    # battery_status is core (always advertised) so search must never surface it,
    # even when the intent matches it.
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    names = _lexical(reg).search("send a slack message")
    assert names == ["slack__send"]


def test_lexical_search_excludes_core_even_when_intent_matches_core() -> None:
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    assert _lexical(reg).search("what's my battery level") == []  # only core matched → no gated


def test_lexical_search_respects_core_extra_remove() -> None:
    reg = _reg()  # battery_status + set_volume core; slack__send + github__issue gated
    # Promote slack__send to core (so search hides it) and demote set_volume to gated.
    selector = LexicalToolSelector(
        reg,
        budget=20,
        core_extra=frozenset({"slack__send"}),
        core_remove=frozenset({"set_volume"}),
    )
    names = selector.search("send a slack message and set the volume")
    assert "slack__send" not in names  # promoted to core → excluded from search
    assert "set_volume" in names  # demoted to gated → now eligible


def test_search_honors_limit() -> None:
    reg = ToolRegistry()
    for i in range(5):
        reg.register(_spec(f"slack__send_{i}", "Send a message to a Slack channel."))
    names = _lexical(reg).search("send a slack message", limit=2)
    assert len(names) == 2


def test_search_empty_intent_returns_empty() -> None:
    assert _lexical(_reg()).search("") == []
    assert AllToolsSelector(_reg()).search("") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_tool_selection.py -k "search" -v`
Expected: FAIL — `AttributeError: 'AllToolsSelector' object has no attribute 'search'` (and the same for `LexicalToolSelector`).

- [ ] **Step 3: Implement `search` on `AllToolsSelector`**

In `src/autobot/tools/selection.py`, in `AllToolsSelector`, add this method immediately after `select`:

```python
    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Top ``limit`` tool names by relevance to ``intent`` (all tools ranked).

        The "all" mode draws no core/gated line, so every registered tool is a
        candidate. Used only as the ``find_tools`` backend when gating is disabled.
        """
        ranked = score_tools(intent, self._registry.specs())
        return [spec.name for spec, _ in ranked[:limit]]
```

- [ ] **Step 4: Implement `search` on `LexicalToolSelector`**

In `src/autobot/tools/selection.py`, in `LexicalToolSelector`, add this method immediately after `select`:

```python
    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Top ``limit`` *gated* tool names by relevance to ``intent``.

        Core tools are excluded — the model already sees them every round, so a
        discovery query should only surface the gated tools it can't currently
        reach. ``core_extra``/``core_remove`` shift the core boundary the same way
        they do in :meth:`select`.
        """
        specs = self._registry.specs()
        core_names = ({s.name for s in specs if s.core} | self._core_extra) - self._core_remove
        gated = [s for s in specs if s.name not in core_names]
        ranked = score_tools(intent, gated)
        return [spec.name for spec, _ in ranked[:limit]]
```

- [ ] **Step 5: Run tests + mypy**

Run: `uv run pytest tests/unit/test_tool_selection.py -v` → PASS (all, incl. the 6 new).
Run: `uv run mypy` → `Success` (now that both classes have `search`, `build_tool_selector`'s `-> ToolSelector` return type is satisfied structurally).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/tools/selection.py tests/unit/test_tool_selection.py
git commit -m "feat(tools): implement ToolSelector.search on the all/lexical selectors"
```

---

### Task 3: The `find_tools` meta-tool spec (schema source, not registered)

**Files:**
- Modify: `src/autobot/tools/builtin.py`
- Test: `tests/unit/test_tools.py`

**Interfaces:**
- Consumes: `ToolSpec`, `Risk.READ_ONLY`.
- Produces:
  - `find_tools(intent: str) -> str` — a module-level handler (a static fallback string; the turn loop owns the live behavior).
  - `FIND_TOOLS: ToolSpec` — the always-advertised meta-tool's name + description + parameters + schema, in one place.

**Wiring decision (and why):** `FIND_TOOLS` is **deliberately not registered** in the `ToolRegistry`.

- *Why not register it?* If it were registered, (a) it would sit in the **gated** pool and could burn a budget slot whenever a query happened to contain "find"/"tools", and could be double-advertised; (b) calls would flow into `ToolRegistry.dispatch` → its handler, which has no access to the selector or the per-turn pin state — so it couldn't actually search-and-pin. Keeping it out of the registry makes the LLM client the single owner of its semantics (search + pin + summary), with zero budget contention and no risk of double-advertising.
- *Why a `ToolSpec` at all (vs. an inline dict in the LLM module)?* The repo convention is "per-tool metadata lives next to the tool." `FIND_TOOLS` is the single source for the name, the model-facing description (which teaches the model *when* to reach for discovery), and `to_schema()`. The LLM module imports it rather than hand-rolling a schema.
- *Why is `find_tools` only on the selector path (not the legacy no-selector path)?* The escape hatch's whole job is to call `selector.search()`. With no selector, the legacy path advertises **every** registered tool already — there is nothing to discover and nothing to pin, so `find_tools` would be both meaningless (no `search` to run) and unnecessary (the model can already see every tool). The simpler correct option is therefore: advertise `find_tools` **only when a selector is wired**. Task 4 implements exactly that.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tools.py`. It already imports `ToolSpec`/`ToolRegistry` and uses `register_builtins`; add the `FIND_TOOLS` import where the other `autobot.tools.builtin` symbols are imported, or import locally as below:

```python
def test_find_tools_spec_is_well_formed() -> None:
    from autobot.tools.builtin import FIND_TOOLS

    assert FIND_TOOLS.name == "find_tools"
    assert "intent" in FIND_TOOLS.parameters["properties"]
    assert FIND_TOOLS.parameters["required"] == ["intent"]
    # The handler returns a string and never raises (tool contract).
    assert isinstance(FIND_TOOLS.handler(intent="anything"), str)


def test_find_tools_is_not_registered_by_register_builtins() -> None:
    from autobot.tools.builtin import register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    # find_tools is owned by the LLM turn loop, not the registry/gate.
    assert reg.get("find_tools") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_tools.py -k find_tools -v`
Expected: FAIL — `ImportError: cannot import name 'FIND_TOOLS' from 'autobot.tools.builtin'`.

- [ ] **Step 3: Add the handler + `FIND_TOOLS` spec**

In `src/autobot/tools/builtin.py`, add the handler and the spec immediately after the `GET_TIME` definition (before `register_builtins`):

```python
def find_tools(intent: str) -> str:
    """Fallback text for the discovery meta-tool.

    The real behavior (search the gated tools, pin the matches, summarize them)
    lives in the LLM turn loop, which intercepts ``find_tools`` calls before they
    reach any registry/gate. This handler exists only so the tool has a valid,
    string-returning handler — it is never dispatched in normal operation.
    """
    return f"Searching for tools matching: {intent}"


FIND_TOOLS = ToolSpec(
    name="find_tools",
    description=(
        "Discover tools that are not currently available to you. Call this the "
        "moment a request needs an action you don't see among your tools (for "
        "example messaging, calendars, code hosting, or any connected app). Pass a "
        "short description of what you want to do as `intent` (e.g. 'send a message "
        "on slack', 'create a github issue'). It returns the matching tools, which "
        "then become available so you can call the right one on your next step. "
        "Prefer this over telling the user you can't do something."
    ),
    parameters={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "A short description of the action you want to perform.",
            }
        },
        "required": ["intent"],
    },
    handler=find_tools,
    risk=Risk.READ_ONLY,
)
```

(`register_builtins` is **unchanged** — `FIND_TOOLS` is intentionally not registered, per the wiring decision above. `Risk` and `ToolSpec` are already imported at the top of `builtin.py`.)

- [ ] **Step 4: Run the test + format + mypy**

Run: `uv run pytest tests/unit/test_tools.py -k find_tools -v` → PASS (2).
Run: `uv run ruff format .` then `uv run mypy` → `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/builtin.py tests/unit/test_tools.py
git commit -m "feat(tools): find_tools meta-tool spec (single schema source, unregistered)"
```

---

### Task 4: Per-turn pinning + `find_tools` interception in `OllamaLanguageModel`

**Files:**
- Modify: `src/autobot/llm/ollama_llm.py`
- Test: `tests/unit/test_ollama_llm.py`

**Interfaces:**
- Consumes: `ToolSelector.search`, `FIND_TOOLS`, `ToolCall`.
- Produces (all internal to `OllamaLanguageModel`, no public-signature change):
  - `self._pinned: set[str]` — names pinned this turn (reset to empty at the start of every `run_turn`).
  - `_tools_for_round()` now: on the selector path, returns `[s.to_schema() for s in selector.select(self._round_query, pinned=frozenset(self._pinned))]` **plus** `FIND_TOOLS.to_schema()` (always, regardless of budget); on the no-selector path, returns `registry.schemas()` (unchanged — no `find_tools`, see Task 3 justification).
  - The turn loop intercepts a `find_tools` call: runs `selector.search(intent)`, adds the results to `self._pinned`, and appends a synthesized `tool` message summarizing the matches — without calling `execute` (the loop owns it; it never touches the gate/registry).

**Context:** `run_turn` sets `self._round_query = user_text` then runs `for _ in range(_MAX_TOOL_ROUNDS):` (lines ~366–399). Inside, after `calls = normalize_tool_calls(message)` and the `not calls` break, the per-call dispatch loop is `for call in calls:` (~line 381). The `find_tools` interception goes at the **top** of that `for call in calls:` body, before the `failed`/`execute` logic.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_ollama_llm.py`. `_FakeOllama`, `_resp`, `_tc`, `LexicalToolSelector` are already imported/defined in that file:

```python
def _battery_slack_registry() -> ToolRegistry:
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
    return reg


def _selector(reg: ToolRegistry) -> LexicalToolSelector:
    return LexicalToolSelector(reg, budget=20, core_extra=frozenset(), core_remove=frozenset())


def test_find_tools_is_always_advertised_with_selector() -> None:
    reg = _battery_slack_registry()
    client = _FakeOllama([_resp(content="100%.")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    model.run_turn("what's my battery?", lambda c: ToolResult(name=c.name, content=""))
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert "find_tools" in advertised  # always, even when no gated tool matched
    assert "battery_status" in advertised  # core
    assert "slack__send" not in advertised  # gated + irrelevant → not yet advertised


def test_find_tools_call_pins_matches_for_next_round() -> None:
    reg = _battery_slack_registry()
    # Round 1: the model can't see a slack tool, so it calls find_tools.
    # Round 2: it should now see slack__send (pinned) and call it.
    # Round 3: final text.
    responses = [
        _resp(tool_calls=[_tc("find_tools", {"intent": "send a message on slack"})]),
        _resp(tool_calls=[_tc("slack__send", {"text": "hi"})]),
        _resp(content="Sent your message."),
    ]
    client = _FakeOllama(responses)
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    executed: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call.name)
        return ToolResult(name=call.name, content="sent", ok=True)

    reply = model.run_turn("tell the team hi on slack", execute)
    assert reply == "Sent your message."
    # find_tools was NOT dispatched through the executor (the loop owns it):
    assert executed == ["slack__send"]
    # Round 2's advertised set includes the pinned slack__send:
    round2_tools = {t["function"]["name"] for t in client.calls[1]["tools"]}
    assert "slack__send" in round2_tools
    # The find_tools result was fed back as a tool message in round 2's prompt:
    round2_msgs = client.calls[1]["messages"]
    assert any(
        m.get("role") == "tool" and m.get("tool_name") == "find_tools" for m in round2_msgs
    )


def test_pins_reset_between_turns() -> None:
    reg = _battery_slack_registry()
    responses = [
        _resp(tool_calls=[_tc("find_tools", {"intent": "send a message on slack"})]),
        _resp(content="Found it."),  # turn 1, round 2: final
        _resp(content="100%."),  # turn 2, round 1: final
    ]
    client = _FakeOllama(responses)
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    model.run_turn("message slack", lambda c: ToolResult(name=c.name, content="", ok=True))
    assert "slack__send" in model._pinned  # pinned during turn 1
    model.run_turn("what's my battery?", lambda c: ToolResult(name=c.name, content="", ok=True))
    assert model._pinned == set()  # reset at the start of turn 2
    # Turn 2's first round must NOT carry turn 1's pin into the advertised set:
    turn2_tools = {t["function"]["name"] for t in client.calls[-1]["tools"]}
    assert "slack__send" not in turn2_tools
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_ollama_llm.py -k "find_tools or pins" -v`
Expected: FAIL — `find_tools` not in the advertised set (no `FIND_TOOLS` appended yet), and `AttributeError: 'OllamaLanguageModel' object has no attribute '_pinned'`.

- [ ] **Step 3: Import `FIND_TOOLS` and init the pin set**

In `src/autobot/llm/ollama_llm.py`, add the import next to the other `autobot.tools` imports (after `from autobot.tools.registry import ToolRegistry`):

```python
from autobot.tools.builtin import FIND_TOOLS
```

Then, in `OllamaLanguageModel.__init__`, immediately after `self._round_query = ""  # current turn's user text; the relevance signal`, add:

```python
        self._pinned: set[str] = set()  # tools discovered via find_tools, this turn only
```

- [ ] **Step 4: Pass `pinned` and always append `find_tools` in `_tools_for_round`**

In `src/autobot/llm/ollama_llm.py`, replace the body of `_tools_for_round` with:

```python
    def _tools_for_round(self) -> list[dict[str, Any]]:
        """Schemas to advertise this round: the selector's subset, or all tools.

        With a selector wired, advertise the relevance-gated subset for this turn's
        message (including any tools pinned by ``find_tools`` so far), plus the
        always-on ``find_tools`` meta-tool so the model can discover what the
        pre-filter missed. Without a selector, every registered tool is advertised —
        the original behavior, kept so existing callers/tests are unaffected (and
        ``find_tools`` is pointless there: nothing is gated, nothing to discover).
        """
        if self._selector is None:
            return self._registry.schemas()
        selected = self._selector.select(self._round_query, pinned=frozenset(self._pinned))
        return [spec.to_schema() for spec in selected] + [FIND_TOOLS.to_schema()]
```

- [ ] **Step 5: Reset pins per turn + intercept `find_tools` in the loop**

In `src/autobot/llm/ollama_llm.py`, in `run_turn`, immediately after `self._round_query = user_text  # relevance signal for tool selection this turn`, add:

```python
        self._pinned = set()  # find_tools discoveries are per-turn; never leak across turns
```

Then, inside the `for call in calls:` loop, insert the interception as the **first** statement of the loop body — i.e. immediately after `for call in calls:` and before the existing `key = call.name + ...` line:

```python
            if call.name == FIND_TOOLS.name and self._selector is not None:
                all_repeat = False  # discovery is real progress, not a failing repeat
                summary = self._discover_tools(call.arguments.get("intent", ""))
                messages.append(
                    {"role": "tool", "tool_name": call.name, "content": summary}
                )
                continue
```

Then add this helper method to the class (e.g. immediately after `_tools_for_round`):

```python
    def _discover_tools(self, intent: str) -> str:
        """Run the find_tools escape hatch: search, pin matches, summarize for the model.

        Asks the selector for the gated tools best matching ``intent``, pins their
        names so :meth:`_tools_for_round` advertises them for the rest of this turn,
        and returns a short ``name: description`` summary the model can read to pick
        the right tool on its next step. With no selector (legacy path) ``find_tools``
        is never advertised, so this is only reached when ``self._selector`` is set.
        """
        if self._selector is None:  # defensive; find_tools isn't advertised without one
            return "Tool discovery is unavailable."
        names = self._selector.search(intent)
        self._pinned.update(names)
        specs = [self._registry.get(name) for name in names]
        found = [s for s in specs if s is not None]
        _log.info("find_tools intent=%r matched=%s", intent, [s.name for s in found])
        if not found:
            return f"No tools found for: {intent}. Tell the user you can't do that."
        lines = [f"- {s.name}: {s.description}" for s in found]
        return "Found these tools (now available to call):\n" + "\n".join(lines)
```

- [ ] **Step 6: Run tests + mypy**

Run: `uv run pytest tests/unit/test_ollama_llm.py -v` → PASS (all, incl. the 3 new; the Phase-1 `test_selector_gates_advertised_tools` still passes — `find_tools` is additive, it asserts on `battery_status`/`slack__send` only; `test_no_selector_advertises_all_tools` still passes — the no-selector path is unchanged).
Run: `uv run mypy` → `Success`.

- [ ] **Step 7: Full gate**

Run: `make check`
Expected: PASS (ruff + ruff-format + mypy strict + full pytest suite all green).

- [ ] **Step 8: Commit**

```bash
git add src/autobot/llm/ollama_llm.py tests/unit/test_ollama_llm.py
git commit -m "feat(llm): find_tools per-turn pinning + always-advertised escape hatch"
```

---

## Manual smoke-test (after Task 4)

Optional, with a live Ollama and `allow_mcp` + an MCP server enabled (e.g. Slack):

1. `make run`, then ask **"hi"** — confirm the reply is normal and the advertised set is still small (core + `find_tools`; no gated/MCP schemas). The context line should match Phase 1's baseline plus the one `find_tools` schema.
2. Ask for something whose tool the lexical pre-filter is likely to **miss** by paraphrase but a connected MCP server provides (e.g. "tell the team we're shipping" with Slack connected). Watch `~/.autobot/logs/autobot.log` (`make logs-grep C=llm`): you should see a `planned tools=['find_tools']` round, then a `find_tools intent=... matched=[...]` line, then a follow-up round where the model calls the discovered MCP tool — all within the **one** turn.
3. Ask a second, unrelated question in the same session (e.g. "what's my battery?") — confirm it answers and that the previously-pinned tool is **not** advertised (pins reset between turns).
4. Set `"tool_selection": "all"` in `~/.autobot/settings.json`, restart, and confirm `find_tools` is **not** advertised (the no-selector/all path advertises everything, so discovery is unnecessary) and behavior matches Phase-1 "all" mode.

---

## Self-Review

**1. Spec coverage** (design §11 P2: "`find_tools` meta-tool + per-turn pinning in the turn loop"; §4d, §5, §10):
- `ToolSelector.search(intent, *, limit=5) -> list[str]` added to the Protocol → Task 1 ✓ (matches design §4b exactly).
- `search` implemented on both `AllToolsSelector` (all specs ranked) and `LexicalToolSelector` (gated-only, core excluded), via the Phase-1 `score_tools` → Task 2 ✓.
- `find_tools(intent) -> str` meta-tool, always advertised; cleanest wiring documented (unregistered `ToolSpec` constant; loop owns search-and-pin) → Tasks 3 + 4 ✓.
- Per-turn `self._pinned` reset at the start of each `run_turn`; loop intercepts `find_tools`, fills `self._pinned` via `selector.search`, returns a name+one-line-description summary; `_tools_for_round` passes `pinned=frozenset(self._pinned)` into `select(...)` **and** always appends the `find_tools` schema → Task 4 ✓.
- `find_tools` always advertised on the selector path regardless of budget (appended after `select`, never bounded) → Task 4 ✓. On the no-selector legacy path it is intentionally **not** advertised; justified (nothing gated → nothing to discover) → Task 3 wiring decision ✓.
- Per-turn data flow (design §5): reset pins → `select(query, pinned={})` + `find_tools` → real-tool calls go through `execute`/gate unchanged → a `find_tools` call runs `search`, pins, summarizes (never through `execute`) → next round `select(query, pinned)` includes the pinned tools → normal termination → Task 4 ✓.
- Risk "Pinned-state leakage across turns" (design §10) → `self._pinned = set()` at the top of `run_turn`, asserted by `test_pins_reset_between_turns` → Task 4 ✓.
- Tests proving: `search` ranks gated tools (Task 2); a `find_tools` call pins matches that appear in the next round's advertised set asserted via `_FakeOllama.calls` (Task 4, `test_find_tools_call_pins_matches_for_next_round`); pins reset between turns (Task 4, `test_pins_reset_between_turns`); `find_tools` always advertised (Task 4, `test_find_tools_is_always_advertised_with_selector`) ✓.

**2. Placeholder scan:** none — every code step shows complete, runnable code; every run step states the exact command and expected result. Task 1's "run mypy between tasks" caveat is explained, not a placeholder (the `python -c` import is its real gate; Task 2 closes the structural-conformance loop in the same module). The only "find the location" steps reference exact, verified anchor lines from the current source (`self._round_query = ""` in `__init__`; `self._round_query = user_text` and `for call in calls:` in `run_turn`; the `_tools_for_round` body; `register_builtins` in `builtin.py`).

**3. Type consistency:**
- `ToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` — identical in the Protocol (Task 1), `AllToolsSelector` (Task 2), `LexicalToolSelector` (Task 2), and the call site `self._selector.search(intent)` in `_discover_tools` (Task 4, returns `list[str]` → `set[str].update(...)`).
- `score_tools(query, specs) -> list[tuple[ToolSpec, float]]` — consumed identically in both `search` impls: `[spec.name for spec, _ in score_tools(...)[:limit]]` yields `list[str]`.
- `FIND_TOOLS: ToolSpec` (Task 3) → `FIND_TOOLS.name: str`, `FIND_TOOLS.to_schema() -> dict[str, Any]` (matches the `list[dict[str, Any]]` return of `_tools_for_round`), `FIND_TOOLS.handler(intent=...) -> str`.
- `self._pinned: set[str]` (Task 4) → `frozenset(self._pinned)` passed to `select(..., pinned: frozenset[str])` (Phase-1 signature) — type-exact; `.update(list[str])` is valid on `set[str]`.
- `self._selector: ToolSelector | None` (Phase 1) — the `find_tools` interception guards on `self._selector is not None`, and `_discover_tools` re-guards (defensive) so mypy narrows before `.search`/`.select`.
- `ToolRegistry.get(name) -> ToolSpec | None` — `_discover_tools` filters `None` out (`found = [s for s in specs if s is not None]`) before reading `s.name`/`s.description`, so the list comprehension types as `list[ToolSpec]`.
- No public signature changed: `OllamaLanguageModel.__init__` and `run_turn` keep their Phase-1 signatures, so `app.py::_build_llm` and every existing test/caller is unaffected.

No issues found.

---

## Cross-phase interface decisions (for the Phase 3 / Phase 4 planners)

- **`ToolSelector.search(self, intent: str, *, limit: int = 5) -> list[str]` is now on the Protocol and stable.** Phase 4's `EmbeddingToolSelector` must implement **both** `select(query, *, pinned=frozenset())` and this `search`. `search` returns bare tool **names**, **excludes core tools**, and ranks most-relevant-first (empty on no match). Phase 4 may rank by cosine similarity instead of `score_tools`, but the signature, the name-only return, and the core-exclusion contract must hold.
- **`find_tools` is owned by the LLM turn loop, not the registry.** It is a module-level `ToolSpec` constant (`autobot.tools.builtin.FIND_TOOLS`) that is **never registered** and **never dispatched through the gate**; the loop intercepts it. Phase 3's Anthropic path should **not** mirror this shim — per design §6 it delegates discovery to the native Tool Search Tool (`defer_loading` + `tool_search_tool_*`), so `FIND_TOOLS` is a local/Ollama-only construct. If Phase 3 ever needs a fallback discovery path, it can reuse `selector.search` directly.
- **Pin state is per-turn and private to `OllamaLanguageModel` (`self._pinned`, reset in `run_turn`).** It is intentionally not part of any protocol or the `LanguageModel` interface. Phase 3/4 should keep discovery/pinning local to each client implementation rather than promoting it to a shared interface.
- **`find_tools` is advertised only on the selector path** (when `self._selector is not None`). The no-selector/`"all"` path is unchanged and carries no `find_tools`. Any future "always gate" decision should preserve this: discovery only makes sense when something is gated.
