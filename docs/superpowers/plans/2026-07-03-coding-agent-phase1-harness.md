# AgentHarness Foundation (Phase 1a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the duplicated agent loop out of the two provider classes into one reusable `AgentHarness` driving a provider-agnostic `ChatModel`, with assistant behavior byte-for-byte unchanged.

**Architecture:** A new `src/autobot/agent/` package defines a minimal `ChatModel` protocol (provider-native turn primitives: `begin_turn` / `send` / `record_results` / `handle_discovery` / `final_answer_no_tools` / `finalize_turn`, plus `complete` / `context_usage` / `new_session` / `set_delivery_mode` / `context_window`) and an `AgentHarness` that owns the shared round loop (executor dispatch, anti-thrash, discovery forwarding, round-cap → forced final answer, doom-loop detection). Each provider keeps its own native history, caching, trimming, and compaction — only the loop moves. `AgentHarness` exposes the same surface the `Orchestrator` and `ReloadableLanguageModel` already call, so `_build_llm` returns `AgentHarness(model)` and nothing downstream changes.

**Tech Stack:** Python 3.11+, `from __future__ import annotations`, mypy strict, ruff, pytest (explicit fakes, no mocking framework). Ollama + Anthropic SDKs (lazy-imported).

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- mypy runs in **strict** mode — keep it green. Full type hints on every function.
- Google-style docstrings on public modules, classes, functions (ruff pydocstyle `D`); tests exempt.
- Line length 100; formatting/import order owned by ruff (`make format`) — do not hand-format.
- Value objects are `frozen=True, slots=True` dataclasses with no business logic.
- Tools return strings and never raise out of `dispatch`; the harness must not let a tool error crash the loop (route through `execute`, which returns a `ToolResult`).
- Logging: `from autobot.logging_setup import get_logger`; new component tag `[harness]` via `get_logger("harness")`. Log seam events (INFO), not per-round noise.
- Verification: `make check` (ruff + ruff-format + mypy strict + pytest) must pass before every commit.
- Commit messages: Conventional Commits (`feat:`, `refactor:`, `test:`…). **Do NOT add any Co-Authored-By / attribution trailer** (repo convention).
- Behavior-preserving: existing tests in `tests/unit/test_ollama_llm.py`, `test_anthropic_llm.py`, `test_llm_parsing.py`, `test_state_machine.py`, `test_reloadable_llm.py`, `test_language_model_complete.py` must stay green unchanged unless a task explicitly updates one.
- Scope: this plan is Phase 1a (issues #44 + #45). Sessions (#46) and provider/keyring "any LLM" (#47) are separate follow-on plans.

---

## File Structure

- Create `src/autobot/agent/__init__.py` — package marker.
- Create `src/autobot/agent/chat_model.py` — `ChatResponse` value type + `ChatModel` Protocol. One responsibility: the provider seam.
- Create `src/autobot/agent/harness.py` — `AgentHarness`: the shared loop. One responsibility: turn orchestration.
- Modify `src/autobot/llm/ollama_llm.py` — split `run_turn` into `ChatModel` primitives; delete the loop body (moves to the harness).
- Modify `src/autobot/llm/anthropic_llm.py` — same split, preserving caching/pairing/trimming.
- Modify `src/autobot/app.py:282-321` (`_build_llm`) — return `AgentHarness(model)`.
- Create `tests/unit/test_agent_harness.py` — harness loop tests with a `FakeChatModel`.
- Create `tests/unit/test_chat_model_protocol.py` — protocol conformance test.

---

### Task 1: `ChatModel` protocol + `ChatResponse` value type

**Files:**
- Create: `src/autobot/agent/__init__.py`
- Create: `src/autobot/agent/chat_model.py`
- Test: `tests/unit/test_chat_model_protocol.py`

**Interfaces:**
- Consumes: `autobot.core.types.ToolCall`, `ToolResult`.
- Produces:
  - `ChatResponse(text: str, tool_calls: list[ToolCall])` — frozen slots dataclass.
  - `ChatModel` Protocol with methods:
    - `begin_turn(self, user_text: str) -> None`
    - `send(self) -> ChatResponse`
    - `record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None`
    - `handle_discovery(self, call: ToolCall) -> str | None`
    - `final_answer_no_tools(self) -> str`
    - `finalize_turn(self) -> None`
    - `complete(self, prompt: str, *, temperature: float = 0.0) -> str`
    - `context_usage(self) -> dict[str, Any] | None`
    - `new_session(self) -> None`
    - `set_delivery_mode(self, mode: str) -> None`

- [ ] **Step 1: Create the package marker**

Create `src/autobot/agent/__init__.py`:

```python
"""The reusable agent harness and its provider-agnostic model seam."""

from __future__ import annotations
```

- [ ] **Step 2: Write the failing protocol-conformance test**

Create `tests/unit/test_chat_model_protocol.py`:

```python
from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.core.types import ToolCall, ToolResult


class _MinimalModel:
    def begin_turn(self, user_text: str) -> None: ...
    def send(self) -> ChatResponse:
        return ChatResponse(text="hi", tool_calls=[])
    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None: ...
    def handle_discovery(self, call: ToolCall) -> str | None:
        return None
    def final_answer_no_tools(self) -> str:
        return ""
    def finalize_turn(self) -> None: ...
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return ""
    def context_usage(self) -> dict[str, Any] | None:
        return None
    def new_session(self) -> None: ...
    def set_delivery_mode(self, mode: str) -> None: ...


def test_minimal_model_satisfies_chat_model_protocol() -> None:
    assert isinstance(_MinimalModel(), ChatModel)


def test_chat_response_is_frozen() -> None:
    resp = ChatResponse(text="a", tool_calls=[ToolCall(name="t")])
    assert resp.text == "a"
    assert resp.tool_calls[0].name == "t"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_chat_model_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autobot.agent.chat_model'`.

- [ ] **Step 4: Implement `chat_model.py`**

Create `src/autobot/agent/chat_model.py`:

```python
"""The provider-agnostic model seam the :class:`~autobot.agent.harness.AgentHarness` drives.

A :class:`ChatModel` owns its own provider-native conversation history, caching,
trimming, and compaction. The harness only orchestrates the *round loop*, calling
these primitives — so swapping a provider (or adding a new one) never touches the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from autobot.core.types import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """One assistant response: its final text and any tool calls it requested."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class ChatModel(Protocol):
    """A provider adapter exposing turn primitives the harness drives.

    The adapter records the assistant message and tool results in its own native
    history during ``send``/``record_results``; the harness never sees provider
    message shapes. ``handle_discovery`` lets a provider that discovers tools
    *client-side* (e.g. Ollama's ``find_tools``) service such a call inline; a
    provider using server-side search returns ``None``.
    """

    def begin_turn(self, user_text: str) -> None:
        """Start a turn: record the user message and reset per-turn state."""
        ...

    def send(self) -> ChatResponse:
        """Assemble + send the current history, record the assistant reply natively."""
        ...

    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append this round's tool results to the native history, in call order."""
        ...

    def handle_discovery(self, call: ToolCall) -> str | None:
        """Service a client-side tool-discovery call, or ``None`` if not one."""
        ...

    def final_answer_no_tools(self) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        ...

    def finalize_turn(self) -> None:
        """Post-turn housekeeping: compaction, usage reporting, history trim."""
        ...

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot, non-conversational completion (no tools)."""
        ...

    def context_usage(self) -> dict[str, Any] | None:
        """Context-meter payload, or ``None`` before the first turn."""
        ...

    def new_session(self) -> None:
        """Discard conversation history and start fresh."""
        ...

    def set_delivery_mode(self, mode: str) -> None:
        """Set how the next reply is delivered (``"chat"`` = text, else spoken)."""
        ...
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_chat_model_protocol.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Typecheck + lint**

Run: `uv run mypy src/autobot/agent && uv run ruff check src/autobot/agent`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/autobot/agent/__init__.py src/autobot/agent/chat_model.py tests/unit/test_chat_model_protocol.py
git commit -m "feat(agent): add provider-agnostic ChatModel protocol (#45)"
```

---

### Task 2: `AgentHarness` — the shared round loop

**Files:**
- Create: `src/autobot/agent/harness.py`
- Test: `tests/unit/test_agent_harness.py`

**Interfaces:**
- Consumes: `ChatModel`, `ChatResponse` (Task 1); `ToolCall`, `ToolResult`, `ToolExecutor` (`autobot.core.types`).
- Produces: `AgentHarness(model: ChatModel, *, max_rounds: int = 8)` with:
  - `run_turn(self, user_text: str, execute: ToolExecutor) -> str`
  - `complete(self, prompt: str, *, temperature: float = 0.0) -> str` (delegates to model)
  - `context_usage(self) -> dict[str, Any] | None` (delegates)
  - `new_session(self) -> None` (delegates)
  - `set_delivery_mode(self, mode: str) -> None` (delegates)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_agent_harness.py`:

```python
from __future__ import annotations

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness
from autobot.core.types import ToolCall, ToolResult


class FakeChatModel:
    """Scriptable ChatModel: returns queued ChatResponses; records interactions."""

    def __init__(self, responses: list[ChatResponse], *, final: str = "FINAL") -> None:
        self._responses = list(responses)
        self._final = final
        self.recorded: list[list[tuple[ToolCall, ToolResult]]] = []
        self.turns: list[str] = []
        self.finalized = 0

    def begin_turn(self, user_text: str) -> None:
        self.turns.append(user_text)

    def send(self) -> ChatResponse:
        return self._responses.pop(0)

    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        self.recorded.append(results)

    def handle_discovery(self, call: ToolCall) -> str | None:
        return None

    def final_answer_no_tools(self) -> str:
        return self._final

    def finalize_turn(self) -> None:
        self.finalized += 1

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return "ONESHOT"

    def context_usage(self):
        return {"used": 1}

    def new_session(self) -> None: ...
    def set_delivery_mode(self, mode: str) -> None: ...


def _ok_executor(call: ToolCall) -> ToolResult:
    return ToolResult(name=call.name, content=f"ran {call.name}", ok=True)


def test_no_tool_calls_returns_reply_and_finalizes() -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model)
    assert harness.run_turn("hi", _ok_executor) == "hello"
    assert model.turns == ["hi"]
    assert model.finalized == 1


def test_one_tool_round_executes_then_replies() -> None:
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="get_time")]),
            ChatResponse(text="it is noon", tool_calls=[]),
        ]
    )
    seen: list[str] = []

    def exec_(call: ToolCall) -> ToolResult:
        seen.append(call.name)
        return ToolResult(name=call.name, content="noon", ok=True)

    harness = AgentHarness(model)
    assert harness.run_turn("time?", exec_) == "it is noon"
    assert seen == ["get_time"]
    assert model.recorded[0][0][1].content == "noon"


def test_repeated_failing_call_stops_with_failure_text() -> None:
    fail = ChatResponse(text="", tool_calls=[ToolCall(name="boom", arguments={"x": 1})])
    model = FakeChatModel([fail, fail])  # same call twice

    def exec_(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="it broke", ok=False)

    harness = AgentHarness(model)
    # round 1 executes (fails); round 2 re-issues the same call -> all_repeat -> stop.
    assert harness.run_turn("go", exec_) == "it broke"


def test_round_cap_forces_final_answer() -> None:
    loop = ChatResponse(text="", tool_calls=[ToolCall(name="spin", arguments={"n": 1})])
    # Every round asks for a *distinct* tool call so anti-thrash never trips; cap wins.
    responses = [
        ChatResponse(text="", tool_calls=[ToolCall(name="spin", arguments={"n": i})])
        for i in range(8)
    ]
    model = FakeChatModel(responses, final="gave up cleanly")
    harness = AgentHarness(model, max_rounds=8)
    assert harness.run_turn("go", _ok_executor) == "gave up cleanly"


def test_identical_call_repeated_trips_doom_loop_guard() -> None:
    same = lambda: ChatResponse(text="", tool_calls=[ToolCall(name="p", arguments={"a": 1})])
    model = FakeChatModel([same(), same(), same(), same()], final="stopped")
    harness = AgentHarness(model, max_rounds=8, doom_limit=3)
    # succeeds each time (ok=True) so anti-thrash won't stop it; doom guard must.
    reply = harness.run_turn("go", _ok_executor)
    assert reply  # a non-empty explanation, not an infinite loop
    assert len(model.recorded) < 8  # stopped early


def test_delegation_methods_forward_to_model() -> None:
    model = FakeChatModel([ChatResponse(text="x", tool_calls=[])])
    harness = AgentHarness(model)
    assert harness.complete("p") == "ONESHOT"
    assert harness.context_usage() == {"used": 1}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_agent_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autobot.agent.harness'`.

- [ ] **Step 3: Implement `harness.py`**

Create `src/autobot/agent/harness.py`:

```python
"""The one agent loop, shared by every provider.

Extracted verbatim (behavior-preserving) from the duplicated ``run_turn`` loops
that used to live in ``llm/ollama_llm.py`` and ``llm/anthropic_llm.py``. The loop
drives a :class:`~autobot.agent.chat_model.ChatModel`: send → dispatch tool calls
through the injected executor (the permission gate) → feed results back → repeat,
until the model returns no tool calls (the final answer), a round only re-issues
already-failed calls, an identical call repeats past the doom-loop guard, or the
round cap is hit (then one tools-disabled call forces a final answer).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from autobot.core.types import ToolCall, ToolResult
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.agent.chat_model import ChatModel
    from autobot.core.types import ToolExecutor

_log = get_logger("harness")

_MAX_TOOL_ROUNDS = 8  # cap the plan→tool→result loop so it can't spin forever
_DOOM_LIMIT = 4  # abort if one identical (name+args) call repeats this many times


def _call_key(call: ToolCall) -> str:
    """Stable identity for a call (name + canonical args) for anti-thrash/doom checks."""
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)


class AgentHarness:
    """Runs one user turn end-to-end against a :class:`ChatModel`."""

    def __init__(
        self, model: ChatModel, *, max_rounds: int = _MAX_TOOL_ROUNDS, doom_limit: int = _DOOM_LIMIT
    ) -> None:
        self._model = model
        self._max_rounds = max_rounds
        self._doom_limit = doom_limit

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user turn end-to-end; tool calls run through ``execute`` (the gate)."""
        self._model.begin_turn(user_text)
        failed: dict[str, str] = {}  # anti-thrash: call key -> failure text
        seen: dict[str, int] = {}  # doom-loop: call key -> times issued this turn
        reply = ""
        for _ in range(self._max_rounds):
            resp = self._model.send()
            if not resp.tool_calls:
                reply = resp.text
                break
            _log.info("planned tools=%s", [c.name for c in resp.tool_calls])
            results: list[tuple[ToolCall, ToolResult]] = []
            all_repeat = True  # did this round only re-issue calls that already failed?
            last_fail = ""
            doomed = False
            for call in resp.tool_calls:
                discovery = self._model.handle_discovery(call)
                if discovery is not None:
                    all_repeat = False  # discovery is real progress, not a failing repeat
                    results.append((call, ToolResult(name=call.name, content=discovery, ok=True)))
                    continue
                key = _call_key(call)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] >= self._doom_limit:
                    doomed = True
                if key in failed:
                    out, ok = failed[key], False  # already failed — reuse, don't re-run
                    last_fail = out
                else:
                    all_repeat = False
                    result = execute(call)  # through the permission gate
                    out, ok = result.content, result.ok
                    if not result.ok:
                        failed[key] = out
                        last_fail = out
                results.append((call, ToolResult(name=call.name, content=out, ok=ok)))
            self._model.record_results(results)
            if doomed:
                _log.info("stopping: identical tool call repeated past doom-loop guard")
                reply = "I kept trying the same step without progress, so I stopped."
                break
            if all_repeat:  # model is just retrying a failing step — stop and explain
                _log.info("stopping: round repeated only previously-failed tool calls")
                reply = last_fail or "I couldn't complete that, so I stopped."
                break
        else:
            reply = self._model.final_answer_no_tools()
        self._model.finalize_turn()
        return reply

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion — delegated to the model (no tools, no loop)."""
        return self._model.complete(prompt, temperature=temperature)

    def context_usage(self) -> dict[str, Any] | None:
        """Delegate the context-meter payload to the model."""
        return self._model.context_usage()

    def new_session(self) -> None:
        """Delegate session reset to the model."""
        self._model.new_session()

    def set_delivery_mode(self, mode: str) -> None:
        """Delegate delivery-mode selection to the model."""
        self._model.set_delivery_mode(mode)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_agent_harness.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Typecheck + lint**

Run: `uv run mypy src/autobot/agent && uv run ruff check src/autobot/agent`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/agent/harness.py tests/unit/test_agent_harness.py
git commit -m "feat(agent): add AgentHarness owning the shared tool loop (#44)"
```

---

### Task 3: Refactor `OllamaLanguageModel` into a `ChatModel` adapter

Split the existing `run_turn` (`ollama_llm.py:399-470`) into the `ChatModel` primitives. The loop moves to the harness; the send/parse/assemble/compaction stay here. **All existing `test_ollama_llm.py` / `test_llm_parsing.py` tests must remain green** — this is a pure decomposition.

**Files:**
- Modify: `src/autobot/llm/ollama_llm.py`
- Test: `tests/unit/test_ollama_llm_adapter.py` (new)

**Interfaces:**
- Consumes: `ChatResponse` (Task 1).
- Produces: `OllamaLanguageModel` now implements `ChatModel` primitives:
  - `begin_turn(user_text)`, `send() -> ChatResponse`, `record_results(results)`,
    `handle_discovery(call) -> str | None`, `final_answer_no_tools() -> str`, `finalize_turn()`.
  - Keeps: `complete`, `context_usage`, `new_session`, `set_delivery_mode`.
  - **Removes:** the `run_turn` method (the loop now lives in `AgentHarness`).

- [ ] **Step 1: Write the failing adapter tests**

Create `tests/unit/test_ollama_llm_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry


class _FakeOllamaClient:
    """Returns a scripted chat response; records the messages it was sent."""

    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message
        self.sent: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.sent.append(messages)
        return {"message": self._message, "prompt_eval_count": 5, "eval_count": 3}

    def show(self, model: str) -> dict[str, Any]:
        return {"modelinfo": {"qwen2.context_length": 4096}}


def _model(message: dict[str, Any]) -> Any:
    from autobot.llm.ollama_llm import OllamaLanguageModel

    return OllamaLanguageModel(
        Settings(), ToolRegistry(), client=_FakeOllamaClient(message)
    )


def test_ollama_is_a_chat_model() -> None:
    assert isinstance(_model({"content": "hi"}), ChatModel)


def test_begin_then_send_returns_text_when_no_tool_calls() -> None:
    m = _model({"content": "hello there", "tool_calls": []})
    m.begin_turn("hi")
    resp = m.send()
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello there"
    assert resp.tool_calls == []


def test_send_surfaces_tool_calls() -> None:
    m = _model(
        {"content": "", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]}
    )
    m.begin_turn("time?")
    resp = m.send()
    assert [c.name for c in resp.tool_calls] == ["get_time"]


def test_record_results_appends_tool_messages() -> None:
    client = _FakeOllamaClient({"content": "done", "tool_calls": []})
    from autobot.llm.ollama_llm import OllamaLanguageModel

    m = OllamaLanguageModel(Settings(), ToolRegistry(), client=client)
    m.begin_turn("go")
    m.send()
    m.record_results([(ToolCall(name="get_time"), ToolResult(name="get_time", content="noon"))])
    m.send()  # second send must include the tool result in the messages
    roles = [msg.get("role") for msg in client.sent[-1]]
    assert "tool" in roles
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_ollama_llm_adapter.py -v`
Expected: FAIL — `isinstance(..., ChatModel)` is False (methods not yet present) / `AttributeError: 'OllamaLanguageModel' object has no attribute 'begin_turn'`.

- [ ] **Step 3: Add per-turn state fields to `__init__`**

In `src/autobot/llm/ollama_llm.py`, inside `OllamaLanguageModel.__init__` (after `self._summary = ""`, around line 284), add the turn-scoped buffers the split methods share:

```python
        # Per-turn buffers shared by the ChatModel primitives (begin_turn/send/…).
        self._messages: list[dict[str, Any]] = []  # this turn's working message list
        self._sent_start = 0  # index in _messages where this turn's tool exchange begins
        self._user_msg: dict[str, Any] = {}  # this turn's user message (persisted at finalize)
```

- [ ] **Step 4: Add `begin_turn` (moves the pre-flight compaction + assembly)**

Add this method to `OllamaLanguageModel` (place it just above the old `run_turn`, ~line 399). It is the head of the old `run_turn`:

```python
    def begin_turn(self, user_text: str) -> None:
        """Start a turn: reset per-turn state, compact pre-flight, assemble messages."""
        self._user_msg = {"role": "user", "content": user_text}
        self._round_query = user_text  # relevance signal for tool selection this turn
        self._pinned = set()  # find_tools discoveries are per-turn; never leak across turns
        # Proactive: compact BEFORE sending if this prompt would cross the budget.
        estimated = estimate_tokens(self._assemble(self._user_msg))
        self._compact_if_needed(estimated, source="preflight")
        self._messages = self._assemble(self._user_msg)
        self._sent_start = len(self._messages)
```

- [ ] **Step 5: Add `send` (one model call → ChatResponse, records the assistant msg)**

Add:

```python
    def send(self) -> ChatResponse:
        """Call the model once, record the assistant message, return text + tool calls."""
        from autobot.agent.chat_model import ChatResponse

        response = self._chat(self._messages)
        message = _get(response, "message")
        calls = normalize_tool_calls(message)
        self._messages.append(_to_message_dict(message))  # record assistant turn faithfully
        if not calls:
            _log.debug("planned no tool calls model=%s", self._settings.llm_model)
        return ChatResponse(text=message_content(message), tool_calls=calls)
```

- [ ] **Step 6: Add `handle_discovery` (the `find_tools` escape hatch)**

Add:

```python
    def handle_discovery(self, call: ToolCall) -> str | None:
        """Service a ``find_tools`` call inline; ``None`` for any normal tool call."""
        if call.name == FIND_TOOLS.name and self._selector is not None:
            return self._discover_tools(call.arguments.get("intent", ""))
        return None
```

- [ ] **Step 7: Add `record_results` (append tool results in call order)**

Add:

```python
    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append this round's tool results to the working messages, in call order."""
        for call, result in results:
            self._messages.append(
                {"role": "tool", "tool_name": call.name, "content": result.content}
            )
```

- [ ] **Step 8: Add `finalize_turn` (moves the post-loop persistence)**

Add — this is the tail of the old `run_turn`:

```python
    def finalize_turn(self) -> None:
        """Persist this turn append-only, then post-turn compact + report usage."""
        self._history.extend([self._user_msg, *self._messages[self._sent_start :]])
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)
        self._compact_if_needed(self._last_prompt_tokens, source="post-turn")
        self._report_usage()
```

- [ ] **Step 9: Rework `_final_answer_no_tools` to a public no-arg primitive**

Replace the existing `_final_answer_no_tools(self, messages)` (line 501) with a no-arg `final_answer_no_tools` that uses `self._messages`:

```python
    def final_answer_no_tools(self) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        _log.info("tool-round cap reached; forcing a final answer without tools")
        try:
            response = self._chat(self._messages, with_tools=False)
        except Exception:
            _log.exception("forced final answer failed")
            return "Sorry, that took too many steps."
        message = _get(response, "message")
        self._messages.append(_to_message_dict(message))
        return message_content(message) or "Sorry, that took too many steps."
```

- [ ] **Step 10: Delete the old `run_turn` method**

Remove the entire `def run_turn(self, user_text, execute) -> str:` method (old lines 399-470) — the loop now lives in `AgentHarness`. (`complete`, `context_usage`, `new_session`, `set_delivery_mode`, `_compact_if_needed`, `_summarize`, `_report_usage` stay.)

- [ ] **Step 11: Run the new adapter tests + the existing Ollama/parsing tests**

Run: `uv run pytest tests/unit/test_ollama_llm_adapter.py tests/unit/test_llm_parsing.py -v`
Expected: new adapter tests PASS; parsing tests still PASS.

Note: `tests/unit/test_ollama_llm.py` may call the removed `run_turn`. If so, update those cases to drive the model through `AgentHarness(model).run_turn(...)` — the behavior is identical. Run `uv run pytest tests/unit/test_ollama_llm.py -v` and fix any that referenced `run_turn` by wrapping in the harness.

- [ ] **Step 12: Typecheck + commit**

```bash
uv run mypy src/autobot/llm/ollama_llm.py
git add src/autobot/llm/ollama_llm.py tests/unit/test_ollama_llm_adapter.py tests/unit/test_ollama_llm.py
git commit -m "refactor(llm): make OllamaLanguageModel a ChatModel adapter (#45)"
```

---

### Task 4: Refactor `AnthropicLanguageModel` into a `ChatModel` adapter

Same decomposition of `run_turn` (`anthropic_llm.py:714-810`), preserving cache breakpoints, tool_use/tool_result pairing, dynamic trimming, and usage/cost accounting. The delicate `_send` retry/trim logic and `with_cache_breakpoint` are untouched — only the loop skeleton moves out.

**Files:**
- Modify: `src/autobot/llm/anthropic_llm.py`
- Test: `tests/unit/test_anthropic_llm_adapter.py` (new)

**Interfaces:**
- Consumes: `ChatResponse` (Task 1).
- Produces: `AnthropicLanguageModel` implements the same `ChatModel` primitives as Task 3; removes `run_turn`; keeps `complete`, `context_usage`, `new_session`, `set_delivery_mode`, `context_window`, `last_prompt_tokens`.

- [ ] **Step 1: Write the failing adapter test**

Create `tests/unit/test_anthropic_llm_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry


class _Blk:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.usage = _Blk(input_tokens=5, output_tokens=2,
                          cache_read_input_tokens=0, cache_creation_input_tokens=0)


class _FakeMessages:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.calls = 0

    def create(self, **kw: Any) -> _Resp:
        self.calls += 1
        return self._resp


class _FakeAnthropic:
    def __init__(self, resp: _Resp) -> None:
        self.messages = _FakeMessages(resp)

    class models:  # noqa: N801 - mimic SDK attribute
        @staticmethod
        def retrieve(model: str) -> Any:
            return _Blk(max_input_tokens=200_000)


def _model(content: list[Any]) -> Any:
    from autobot.llm.anthropic_llm import AnthropicLanguageModel

    client = _FakeAnthropic(_Resp(content))
    return AnthropicLanguageModel(Settings(), ToolRegistry(), client=client)


def test_anthropic_is_a_chat_model() -> None:
    assert isinstance(_model([_Blk(type="text", text="hi")]), ChatModel)


def test_send_returns_text_when_no_tool_use() -> None:
    m = _model([_Blk(type="text", text="hello")])
    m.begin_turn("hi")
    resp = m.send()
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello"
    assert resp.tool_calls == []


def test_send_surfaces_tool_use() -> None:
    m = _model([_Blk(type="tool_use", id="t1", name="get_time", input={})])
    m.begin_turn("time?")
    resp = m.send()
    assert [c.name for c in resp.tool_calls] == ["get_time"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_anthropic_llm_adapter.py -v`
Expected: FAIL — missing `begin_turn`/`send`.

- [ ] **Step 3: Add per-turn state fields to `__init__`**

In `AnthropicLanguageModel.__init__` (after `self._session_cost_priced = False`, ~line 430), add:

```python
        # Per-turn buffers shared by the ChatModel primitives.
        self._turn_start = 0  # index in _history where this turn began (rollback point)
        self._tools: list[dict[str, Any]] = []  # this turn's assembled tools payload
        self._overhead = 0  # estimated system+tools token overhead this turn
        self._turn_in = 0
        self._turn_out = 0
        self._cache_read = 0
        self._cache_write = 0
        self._prompt_total = 0
        self._turn_failed = False  # a send failed this turn -> loop should end
        self._turn_error = ""  # the reply to return when a send failed
```

- [ ] **Step 4: Add `begin_turn`**

The head of the old `run_turn`:

```python
    def begin_turn(self, user_text: str) -> None:
        """Start a turn: assemble tools, append the user message, reset counters."""
        self._tools = self._assemble_tools(user_text)
        self._overhead = (
            len(self._system()) + sum(len(str(t)) for t in self._tools)
        ) // _CHARS_PER_TOKEN
        self._turn_start = len(self._history)
        self._history.append({"role": "user", "content": user_text})
        self._turn_in = self._turn_out = self._cache_read = self._cache_write = 0
        self._prompt_total = 0
        self._turn_failed = False
        self._turn_error = ""
```

- [ ] **Step 5: Add `send` (one `_send` call, records assistant blocks, tallies usage)**

```python
    def send(self) -> ChatResponse:
        """Send once; record the assistant blocks; return text + tool calls.

        On a cloud failure the turn is abandoned (history rolled back to the start)
        and an empty ChatResponse is returned with the error reply stashed for the
        loop to surface via :meth:`final_answer_no_tools`.
        """
        from autobot.agent.chat_model import ChatResponse

        problem = _first_pairing_problem(self._history)
        if problem:
            _log.error("history integrity broken before send: %s", problem)
        try:
            resp = self._send(self._tools, self._overhead)
        except Exception as exc:  # cloud rejected/unreachable — stay useful
            _log.warning("cloud request failed: %s", exc)
            del self._history[self._turn_start :]  # abandon this turn; keep history valid
            self._turn_failed = True
            self._turn_error = too_long_reply() if is_too_long_error(exc) else cloud_error_reply(exc)
            return ChatResponse(text="", tool_calls=[])
        usage = _get(resp, "usage")
        in_tok = int(_get(usage, "input_tokens") or 0)
        cr = int(_get(usage, "cache_read_input_tokens") or 0)
        cw = int(_get(usage, "cache_creation_input_tokens") or 0)
        self._turn_in += in_tok
        self._turn_out += int(_get(usage, "output_tokens") or 0)
        self._cache_read += cr
        self._cache_write += cw
        self._prompt_total = in_tok + cr + cw
        content = _get(resp, "content") or []
        self._history.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content]}
        )
        self._last_content = content  # kept so record_results can pair by block id
        calls = parse_tool_uses(content)
        return ChatResponse(text=text_from_content(content), tool_calls=calls)
```

Also add `self._last_content: Any = []` to the `__init__` buffers block from Step 3.

- [ ] **Step 6: Add `handle_discovery` (always None — Anthropic uses server-side search)**

```python
    def handle_discovery(self, call: ToolCall) -> str | None:
        """Anthropic discovers tools server-side (Tool Search Tool), so never inline."""
        return None
```

- [ ] **Step 7: Add `record_results` (pair results to the last assistant's tool_use ids)**

The Anthropic result message pairs each `tool_result` to its `tool_use_id`. Map the harness's ordered results onto the tool_use blocks of the last assistant message:

```python
    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append a user message of tool_results, paired to the last tool_use ids by order."""
        use_ids = [
            _get(b, "id") for b in (self._last_content or []) if _get(b, "type") == "tool_use"
        ]
        blocks = [
            {"type": "tool_result", "tool_use_id": uid, "content": res.content}
            for uid, (_call, res) in zip(use_ids, results)
        ]
        self._history.append({"role": "user", "content": blocks})
```

- [ ] **Step 8: Add `final_answer_no_tools` and `finalize_turn`**

```python
    def final_answer_no_tools(self) -> str:
        """Forced final reply when the round cap is hit, or the stashed error reply."""
        if self._turn_failed:
            return self._turn_error
        _log.info("cloud tool-round cap reached; forcing a final answer without tools")
        try:
            resp = self._client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=self._settings.anthropic_max_tokens,
                temperature=self._settings.llm_temperature,
                system=self._system(),
                messages=with_cache_breakpoint(self._history),
            )
        except Exception:
            _log.warning("cloud forced final answer failed")
            return "Sorry, that took too many steps."
        content = _get(resp, "content") or []
        self._history.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content]}
        )
        return text_from_content(content) or "Sorry, that took too many steps."

    def finalize_turn(self) -> None:
        """Record usage, compact if over threshold, trim to the hard backstop."""
        self._last_prompt_total = self._prompt_total
        self._last_cache_read = self._cache_read
        self._last_cache_write = self._cache_write
        self._last_turn_in = self._turn_in + self._cache_write
        self._last_turn_out = self._turn_out
        self._log_usage(
            self._turn_in, self._turn_out, self._cache_read, self._cache_write, self._prompt_total
        )
        self._maybe_compact(self._prompt_total)
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)
```

**Note on the failed-send case:** when `send` sets `self._turn_failed`, the harness will still call `record_results([])` (no tool calls parsed → the harness breaks out with `reply = resp.text = ""`) — trace it: `send` returns empty `ChatResponse`, harness sees no `tool_calls`, sets `reply=""`, breaks, calls `finalize_turn`. That yields `""`, not the error reply. To preserve the old behavior (return the error reply), handle it in `finalize_turn`/return path: have the harness return the model's stashed error. Simplest: in `send`, when `_turn_failed`, return `ChatResponse(text=self._turn_error, tool_calls=[])` so the harness returns it directly. **Use that** — change Step 5's failure `return` to `return ChatResponse(text=self._turn_error, tool_calls=[])` and drop the `final_answer_no_tools` `_turn_failed` branch. Update the Step 5 code accordingly before running tests.

- [ ] **Step 9: Delete the old `run_turn` method**

Remove `def run_turn(self, user_text, execute) -> str:` (old lines 714-810). Keep `_send`, `_assemble_tools`, `_maybe_compact`, `with_cache_breakpoint`, all pure helpers, `complete`, `context_usage`, `new_session`, `context_window`, `last_prompt_tokens`.

- [ ] **Step 10: Run new + existing Anthropic tests**

Run: `uv run pytest tests/unit/test_anthropic_llm_adapter.py tests/unit/test_anthropic_llm.py -v`
Expected: new adapter tests PASS. If `test_anthropic_llm.py` called `run_turn`, update those cases to `AgentHarness(model).run_turn(...)` (identical behavior) and re-run.

- [ ] **Step 11: Typecheck + commit**

```bash
uv run mypy src/autobot/llm/anthropic_llm.py
git add src/autobot/llm/anthropic_llm.py tests/unit/test_anthropic_llm_adapter.py tests/unit/test_anthropic_llm.py
git commit -m "refactor(llm): make AnthropicLanguageModel a ChatModel adapter (#45)"
```

---

### Task 5: Wire the harness into the composition root

Route `_build_llm` to return `AgentHarness(model)`. Because `AgentHarness` exposes `run_turn` / `complete` / `context_usage` / `new_session` / `set_delivery_mode`, the `ReloadableLanguageModel` wrapper and `Orchestrator` need no changes.

**Files:**
- Modify: `src/autobot/app.py:282-321` (`_build_llm`)
- Test: `tests/unit/test_build_llm_harness.py` (new)

**Interfaces:**
- Consumes: `AgentHarness` (Task 2); the two adapters (Tasks 3-4).
- Produces: `_build_llm(...)` returns an `AgentHarness` wrapping the chosen provider adapter.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/unit/test_build_llm_harness.py`:

```python
from __future__ import annotations

from autobot.agent.harness import AgentHarness
from autobot.app import _build_llm
from autobot.config import Settings
from autobot.tools.registry import ToolRegistry


def test_build_llm_returns_a_harness_for_local() -> None:
    # Local provider: no network, no key. The Ollama client is built lazily on first
    # use, so construction here must not touch it — _build_llm returns a harness.
    llm = _build_llm(Settings(llm_provider="ollama"), ToolRegistry(), None, None)
    assert isinstance(llm, AgentHarness)
    assert hasattr(llm, "run_turn")
    assert hasattr(llm, "set_delivery_mode")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_build_llm_harness.py -v`
Expected: FAIL — `_build_llm` returns an `OllamaLanguageModel`, not an `AgentHarness`.

- [ ] **Step 3: Update `_build_llm` to wrap in the harness**

In `src/autobot/app.py`, at the top of `_build_llm` add the import inside the function (keep heavy imports local as the file already does), and wrap both return paths. The current returns are `return llm` (anthropic, line 308) and `return OllamaLanguageModel(...)` (line 321). Change the function to build the adapter, then wrap:

Replace line 321:
```python
    return OllamaLanguageModel(settings, registry, transcript, memory=memory, selector=selector)
```
with:
```python
    from autobot.agent.harness import AgentHarness

    model = OllamaLanguageModel(settings, registry, transcript, memory=memory, selector=selector)
    return AgentHarness(model)
```

And replace the anthropic branch `return llm` (line 308) with:
```python
            from autobot.agent.harness import AgentHarness

            return AgentHarness(llm)
```

Update the function's return type annotation from `-> LanguageModel:` to `-> AgentHarness:` (add `from autobot.agent.harness import AgentHarness` to the module-level imports for the annotation, or use `from __future__ import annotations` which is already present — so a string annotation `-> "AgentHarness"` needs the TYPE_CHECKING import). Add under the existing `if TYPE_CHECKING:` block (or module imports):
```python
from autobot.agent.harness import AgentHarness  # noqa: E402 if needed near other imports
```

- [ ] **Step 4: Run the wiring test + the reloadable test**

Run: `uv run pytest tests/unit/test_build_llm_harness.py tests/unit/test_reloadable_llm.py -v`
Expected: PASS. `ReloadableLanguageModel` wraps the harness transparently (it only calls `run_turn`/`complete`/`context_usage`/`new_session`/`set_delivery_mode`, all present on `AgentHarness`).

- [ ] **Step 5: Full check**

Run: `make check`
Expected: ruff + ruff-format + mypy strict + pytest all pass. Fix any `test_ollama_llm.py` / `test_anthropic_llm.py` / `test_state_machine.py` cases that referenced the removed `run_turn` by driving through the harness.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/app.py tests/unit/test_build_llm_harness.py
git commit -m "refactor(app): build the AgentHarness around the provider adapter (#44)"
```

---

## Self-Review

**1. Spec coverage (Phase 1a = issues #44, #45):**
- #44 (extract `AgentHarness`, Orchestrator unchanged, FakeChatModel test, mypy/ruff green): Tasks 2, 5. ✅
- #45 (`ChatModel` protocol; Ollama + Anthropic become adapters; providers are pure `ChatModel`; adapters unit-tested; no live network): Tasks 1, 3, 4. ✅
- Doom-loop detection (spec §4.1): Task 2 (`doom_limit`). ✅
- Behavior-preserving (spec §11 mitigation "land the harness with the assistant profile first"): entire plan keeps assistant behavior identical. ✅
- Deferred to later plans (correctly out of scope here): per-tool-result compaction, decision-point reminders (spec §4.1), `Session`/transcripts (#46), providers/keyring (#47), code tools + profiles (Phase 2). Noted in Global Constraints.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases" — every code step shows complete code. The one prose caveat (Task 4 Step 8 note on the failed-send path) resolves to an explicit instruction ("Use that — change Step 5's failure return to …"). ✅

**3. Type consistency:** `ChatModel` primitive names are identical across Task 1 (protocol), Task 3 (Ollama), Task 4 (Anthropic): `begin_turn`, `send`, `record_results(list[tuple[ToolCall, ToolResult]])`, `handle_discovery(ToolCall) -> str | None`, `final_answer_no_tools() -> str`, `finalize_turn()`. `ChatResponse(text, tool_calls)` used consistently. `AgentHarness(model, *, max_rounds, doom_limit)` matches its test usage. ✅

---

## Execution Handoff

After this plan is approved, the next slices are their own plans (each independently shippable):
- **Phase 1b** — `Session` (workspace-scoped, JSONL transcripts, cost tracking) — issue #46.
- **Phase 1c** — Any-LLM providers + cross-platform `keyring` (`openai_compatible`/`gemini` adapters, `Provider` config) — issue #47.
- **Phase 2** — code tools, repo map, plan mode, checkpoints, security gate — issues #48–#53.
