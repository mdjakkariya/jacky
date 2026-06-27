# Multi-step plans (chain tools in one turn) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the local (Ollama) backend a true multi-round tool-calling loop at parity with the cloud (Anthropic) backend, force a final answer at the round cap on both, and surface each tool step as it runs in chat and voice.

**Architecture:** Approach 1 — each backend keeps owning its own `run_turn` loop (the `LanguageModel` protocol is unchanged); step-progress is emitted from the one orchestrator seam every tool call already flows through (`Orchestrator._execute`), so it covers both backends with no change in `llm/`.

**Tech Stack:** Python ≥3.11 (mypy strict, ruff), `ollama` + `anthropic` clients, a thread-safe `EventBus` fanned out over a WebSocket, vanilla HTML/JS chat drawer (`ui/orb/chat.html`).

Design reference: [`docs/plans/autobot_multi_step_tools_plan.md`](../../plans/autobot_multi_step_tools_plan.md). Closes #6.

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- **mypy strict must stay green**; full type hints on all new code.
- Google-style docstrings on public modules/classes/functions (ruff `D` rules; tests exempt). Line length 100. Run `make format`; never hand-format.
- **On-device only, English only.** No new off-device calls or dependencies.
- Tools return strings and never raise out of `dispatch`; the loop must never let a tool error crash the turn.
- `SYSTEM_PROMPT` holds only short, general principles; per-tool guidance lives in each `ToolSpec.description`.
- Commits: **Conventional Commits**, DCO sign-off (`git commit -s`), and **no `Co-Authored-By` trailer**.
- `make check` (ruff + ruff-format + mypy strict + pytest) must pass before each commit.
- Round cap constant value: **8** on both backends.

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/autobot/llm/ollama_llm.py` | local backend loop + prompt | Modify: multi-round loop, `_final_answer_no_tools`, injectable `client`, `_MAX_TOOL_ROUNDS`, `SYSTEM_PROMPT` principle |
| `src/autobot/llm/anthropic_llm.py` | cloud backend loop | Modify: force-final-answer at cap, `_final_answer_no_tools` |
| `src/autobot/core/events.py` | engine→UI event seam | Modify: `StepEvent` + `EventBus.publish_step` |
| `src/autobot/orchestrator/state_machine.py` | per-step emission + voice cue | Modify: `on_step`, `_emit_step`, `_step_label`, per-step deduped voice ack |
| `src/autobot/app.py` | composition root | Modify: thread `on_step` into `Orchestrator` |
| `src/autobot/daemon/runner.py` | daemon wiring | Modify: `publish_step` callback → `bus.publish_step` |
| `ui/orb/chat.html` | chat drawer | Modify: render a live step trace on `type:"step"` |
| `tests/unit/test_ollama_llm.py` | local loop tests | **Create** |
| `tests/unit/test_anthropic_llm.py` | cloud cap test | Modify: add force-final test |
| `tests/unit/test_events.py` | event serialization | Modify: add `publish_step` test |
| `tests/unit/test_state_machine.py` | orchestrator emission | Modify: add `_execute` step + voice-dedupe tests |

---

### Task 1: Local Ollama multi-round tool loop + force-final-answer

**Files:**
- Modify: `src/autobot/llm/ollama_llm.py`
- Test: `tests/unit/test_ollama_llm.py` (create)

**Interfaces:**
- Consumes: existing `_assemble`, `_chat(messages, *, with_tools=True)`, `normalize_tool_calls`, `message_content`, `_to_message_dict`, `_get`, `trim_history`, `_compact_if_needed`, `_report_usage`, `estimate_tokens`.
- Produces: `OllamaLanguageModel(settings, registry, transcript=None, memory=None, client=None)` (new optional `client`); module constant `_MAX_TOOL_ROUNDS = 8`; method `_final_answer_no_tools(messages: list[dict[str, Any]]) -> str`. `run_turn` signature unchanged.

- [ ] **Step 1: Make the client injectable for tests.** In `OllamaLanguageModel.__init__`, add a final keyword param and use it when given. Replace:

```python
    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        from ollama import Client

        self._settings = settings
        self._registry = registry
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        self._client = Client(host=settings.ollama_host)
```

with:

```python
    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        if client is not None:  # injected (tests)
            self._client = client
        else:
            from ollama import Client

            self._client = Client(host=settings.ollama_host)
```

- [ ] **Step 2: Add the round-cap constant.** Below `_HARD_MAX_MESSAGES = 100`, add:

```python
_MAX_TOOL_ROUNDS = 8  # cap the plan→tool→result loop so it can't spin forever (cloud parity)
```

- [ ] **Step 3: Write the failing tests.** Create `tests/unit/test_ollama_llm.py`:

```python
"""Tests for the Ollama backend's multi-round tool loop, with a fake client.

No Ollama server: a fake client returns canned chat responses, so the loop is
exercised entirely offline. Mirrors the pattern in test_anthropic_llm.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.tools.registry import ToolRegistry, ToolSpec


def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": args}}


def _resp(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    msg = {"role": "assistant", "content": content, "tool_calls": tool_calls or []}
    return SimpleNamespace(message=msg, prompt_eval_count=10, eval_count=5)


class _FakeOllama:
    """Returns queued chat responses; records the messages it was called with."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def show(self, _model: str) -> dict[str, Any]:  # _resolve_context fallback
        return {}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="list_files",
            description="List files",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=lambda path="": f"listed {path}",
        )
    )
    reg.register(
        ToolSpec(
            name="open_path",
            description="Open a path",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=lambda path="": f"opened {path}",
        )
    )
    return reg


def _model(responses: list[Any]) -> OllamaLanguageModel:
    # context_tokens override skips the client.show() lookup path entirely.
    return OllamaLanguageModel(
        Settings(context_tokens=4096), _registry(), client=_FakeOllama(responses)
    )


def test_run_turn_no_tools_returns_text() -> None:
    model = _model([_resp(content="Hello there.")])
    assert model.run_turn("hi", lambda c: ToolResult(name=c.name, content="")) == "Hello there."


def test_run_turn_chains_two_tools_in_one_turn() -> None:
    # Round 1: list_files. Round 2 (using the result): open_path. Round 3: final text.
    responses = [
        _resp(tool_calls=[_tc("list_files", {"path": "~/Downloads"})]),
        _resp(tool_calls=[_tc("open_path", {"path": "~/Downloads/latest.png"})]),
        _resp(content="Opened your latest screenshot."),
    ]
    model = _model(responses)
    executed: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call.name)
        return ToolResult(name=call.name, content="ok", ok=True)

    reply = model.run_turn("open my latest screenshot", execute)
    assert reply == "Opened your latest screenshot."
    assert executed == ["list_files", "open_path"]  # chained across rounds


def test_run_turn_does_not_rerun_a_failing_tool_call() -> None:
    # The model re-issues the same failing call; the loop runs it once, then stops.
    responses = [
        _resp(tool_calls=[_tc("open_path", {"path": "/nope"})]),
        _resp(tool_calls=[_tc("open_path", {"path": "/nope"})]),
    ]
    model = _model(responses)
    runs = {"n": 0}

    def execute(call: ToolCall) -> ToolResult:
        runs["n"] += 1
        return ToolResult(name=call.name, content="No access. Do NOT retry.", ok=False)

    reply = model.run_turn("open it", execute)
    assert runs["n"] == 1  # the identical repeat was short-circuited
    assert "do not retry" in reply.lower()


def test_run_turn_forces_final_answer_at_round_cap() -> None:
    # 8 rounds all ask for a (distinct) tool, never converging; at the cap a final
    # tools-disabled call synthesizes the reply (not a canned apology).
    responses = [_resp(tool_calls=[_tc("list_files", {"path": f"/p{i}"})]) for i in range(8)]
    responses.append(_resp(content="Here's what I found so far."))  # forced final, no tools
    model = _model(responses)
    reply = model.run_turn("dig forever", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    assert reply == "Here's what I found so far."
    # The final call was made with tools disabled.
    assert "tools" not in model._client.calls[-1]


def test_history_keeps_tool_messages_across_turns() -> None:
    # Turn 1 runs a tool; turn 2 must see the prior tool exchange in the sent messages.
    model = _model(
        [
            _resp(tool_calls=[_tc("open_path", {"path": "~/a"})]),
            _resp(content="Opened it."),
            _resp(content="Closed it."),
        ]
    )
    model.run_turn("open a", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    model.run_turn("close it", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    sent = model._client.calls[-1]["messages"]
    roles = [m.get("role") for m in sent]
    assert "tool" in roles  # the prior turn's tool result is carried into turn 2
```

- [ ] **Step 4: Run the tests to verify they fail.**

Run: `uv run pytest tests/unit/test_ollama_llm.py -v`
Expected: FAIL — `test_run_turn_chains_two_tools_in_one_turn` (only `list_files` runs; round-2 call dropped), `test_run_turn_does_not_rerun_a_failing_tool_call`, and `test_run_turn_forces_final_answer_at_round_cap` fail against the current single-round `run_turn`.

- [ ] **Step 5: Replace `run_turn` with the multi-round loop.** In `src/autobot/llm/ollama_llm.py`, replace the entire current `run_turn` body (from `user_msg = {...}` through `return reply`) with:

```python
    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user turn end-to-end; see the interface for the contract.

        Runs a bounded multi-round tool loop: the model may call tools, see their
        results, and decide the next action, until it returns no tool calls (the
        final answer) or the round cap is hit (then one tools-disabled call forces a
        final answer). A tool call that already failed this turn is reused, never
        re-run, so a flapping step can't spin the loop.
        """
        user_msg = {"role": "user", "content": user_text}

        # Proactive: compact BEFORE sending if this prompt would cross the budget.
        estimated = estimate_tokens(self._assemble(user_msg))
        self._compact_if_needed(estimated, source="preflight")

        messages = self._assemble(user_msg)
        sent_start = len(messages)  # everything appended below is this turn's tool exchange
        failed: dict[str, str] = {}  # anti-thrash: name+args -> failure text
        reply = ""
        for _ in range(_MAX_TOOL_ROUNDS):
            response = self._chat(messages)
            message = _get(response, "message")
            calls = normalize_tool_calls(message)
            # Record the assistant turn faithfully (text and/or tool_calls).
            messages.append(_to_message_dict(message))
            if not calls:
                _log.debug("planned no tool calls model=%s", self._settings.llm_model)
                reply = message_content(message)
                break
            _log.info(
                "planned tools=%s model=%s", [c.name for c in calls], self._settings.llm_model
            )
            all_repeat = True  # did this round only re-issue calls that already failed?
            last_fail = ""
            for call in calls:
                key = call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)
                if key in failed:
                    out = failed[key]  # already failed — reuse, don't re-run
                    last_fail = out
                else:
                    all_repeat = False
                    # Execution goes through the injected executor (the permission
                    # gate), never the registry directly — this is the gate's seam.
                    result = execute(call)
                    out = result.content
                    if not result.ok:
                        failed[key] = out
                        last_fail = out
                # In call order: the native API pairs results to calls by order.
                messages.append({"role": "tool", "tool_name": call.name, "content": out})
            if all_repeat:  # model is just retrying a failing step — stop and explain
                _log.info("stopping: round repeated only previously-failed tool calls")
                reply = last_fail or "I couldn't complete that, so I stopped."
                break
        else:
            reply = self._final_answer_no_tools(messages)

        # Persist this turn append-only: the user message + everything appended in the
        # loop (assistant tool_calls and each tool result), so a later turn has a real
        # record of what was done and the KV-cache prefix stays stable.
        self._history.extend([user_msg, *messages[sent_start:]])
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)  # hard backstop
        self._compact_if_needed(self._last_prompt_tokens, source="post-turn")
        self._report_usage()
        return reply

    def _final_answer_no_tools(self, messages: list[dict[str, Any]]) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit.

        The history ends with the last round's tool results, so a tool-free call
        yields a clean final reply. Appends the assistant message so it is persisted.
        """
        _log.info("tool-round cap reached; forcing a final answer without tools")
        try:
            response = self._chat(messages, with_tools=False)
        except Exception:
            _log.exception("forced final answer failed")
            return "Sorry, that took too many steps."
        message = _get(response, "message")
        messages.append(_to_message_dict(message))
        return message_content(message) or "Sorry, that took too many steps."
```

- [ ] **Step 6: Run the tests to verify they pass.**

Run: `uv run pytest tests/unit/test_ollama_llm.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 7: Verify the whole suite + types stay green.**

Run: `make check`
Expected: PASS (ruff, ruff-format, mypy strict, pytest).

- [ ] **Step 8: Commit.**

```bash
git add src/autobot/llm/ollama_llm.py tests/unit/test_ollama_llm.py
git commit -s -m "feat(llm): multi-round tool loop for the local backend"
```

---

### Task 2: Force a final answer at the cloud round cap

**Files:**
- Modify: `src/autobot/llm/anthropic_llm.py`
- Test: `tests/unit/test_anthropic_llm.py`

**Interfaces:**
- Consumes: existing `_system`, `with_cache_breakpoint`, `_block_to_dict`, `text_from_content`, `_get`, `self._history`, `self._client.messages.create`.
- Produces: method `_final_answer_no_tools(self) -> str`. Loop `else` branch returns it instead of the canned line.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/test_anthropic_llm.py`:

```python
def test_run_turn_forces_final_answer_at_round_cap() -> None:
    # 8 rounds each request a (distinct) tool and never finish; at the cap a final
    # tools-disabled call synthesizes the reply, not the canned "too many steps" line.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id=f"t{i}", name="open_app", input={"name": f"X{i}"})],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )
        for i in range(8)
    ]
    responses.append(SimpleNamespace(content=[_block(type="text", text="Here's what I managed.")]))
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    reply = model.run_turn("loop", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    assert reply == "Here's what I managed."  # forced final answer, not the canned line
    # The 9th (final) create was made with no tools.
    assert "tools" not in model._client.messages.calls[-1]
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_anthropic_llm.py::test_run_turn_forces_final_answer_at_round_cap -v`
Expected: FAIL with `assert "Sorry, that took too many steps." == "Here's what I managed."`.

- [ ] **Step 3: Add the helper and change the `else` branch.** In `src/autobot/llm/anthropic_llm.py`, change the loop's `else` clause from:

```python
        else:
            reply = reply or "Sorry, that took too many steps."
```

to:

```python
        else:
            reply = self._final_answer_no_tools()
```

and add this method to `AnthropicLanguageModel` (next to `run_turn`):

```python
    def _final_answer_no_tools(self) -> str:
        """One tools-disabled call to synthesize a final reply when the cap is hit.

        The history ends with the last round's tool_results (a complete pairing), so
        a tool-free request yields a clean final reply. Appends the assistant message
        so the history stays faithful. Falls back to a short line on failure.
        """
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
        self._history.append({"role": "assistant", "content": [_block_to_dict(b) for b in content]})
        return text_from_content(content) or "Sorry, that took too many steps."
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `uv run pytest tests/unit/test_anthropic_llm.py -v`
Expected: PASS (the new test plus the existing suite).

- [ ] **Step 5: Commit.**

```bash
git add src/autobot/llm/anthropic_llm.py tests/unit/test_anthropic_llm.py
git commit -s -m "feat(llm): force a final answer at the cloud round cap"
```

---

### Task 3: Multi-step prompting principle

**Files:**
- Modify: `src/autobot/llm/ollama_llm.py` (the shared `SYSTEM_PROMPT`)
- Test: `tests/unit/test_llm_parsing.py`

**Interfaces:**
- Consumes: existing `system_prompt(mode)` / `SYSTEM_PROMPT`.
- Produces: no API change; one added principle line in `SYSTEM_PROMPT`.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/test_llm_parsing.py`:

```python
def test_system_prompt_teaches_multi_step_and_stopping() -> None:
    from autobot.llm.ollama_llm import system_prompt

    text = system_prompt("chat").lower()
    assert "several steps" in text  # encourages chaining within a turn
    assert "final answer" in text  # tells it to stop once it has enough
    assert "failed" in text  # don't repeat a call that already failed
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_llm_parsing.py::test_system_prompt_teaches_multi_step_and_stopping -v`
Expected: FAIL (phrases absent).

- [ ] **Step 3: Add the principle.** In `SYSTEM_PROMPT`, insert this bullet immediately after the first principle (the "You ACT through your tools…" block), before "- Pick the tool…":

```python
        "- You can take several steps in one turn: call a tool, look at its "
        "result, then decide the next action — use one tool's output to inform the "
        "next. Only call a tool when you actually need to act or look something up; "
        "otherwise just answer. Don't repeat a call that already failed. Once you "
        "have what you need, give your final answer.\n"
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `uv run pytest tests/unit/test_llm_parsing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/autobot/llm/ollama_llm.py tests/unit/test_llm_parsing.py
git commit -s -m "feat(llm): teach multi-step tool chaining in the system prompt"
```

---

### Task 4: `StepEvent` + `EventBus.publish_step`

**Files:**
- Modify: `src/autobot/core/events.py`
- Test: `tests/unit/test_events.py`

**Interfaces:**
- Produces: `StepEvent(index: int, tool: str, label: str, status: str)` with `.message()`; `EventBus.publish_step(index: int, tool: str, label: str, status: str) -> None`. Wire shape `{"type":"step","index":int,"tool":str,"label":str,"status":str}` where `status` is `"running" | "done" | "failed"`.

- [ ] **Step 1: Write the failing test.** Add to `tests/unit/test_events.py`:

```python
def test_publish_step_emits_running_then_done() -> None:
    from autobot.core.events import EventBus

    bus = EventBus()
    seen: list[dict[str, object]] = []
    bus.subscribe(seen.append)
    bus.publish_step(0, "search_files", "Searching files", "running")
    bus.publish_step(0, "search_files", "Searching files", "done")
    assert seen == [
        {"type": "step", "index": 0, "tool": "search_files", "label": "Searching files", "status": "running"},
        {"type": "step", "index": 0, "tool": "search_files", "label": "Searching files", "status": "done"},
    ]
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `uv run pytest tests/unit/test_events.py::test_publish_step_emits_running_then_done -v`
Expected: FAIL with `AttributeError: 'EventBus' object has no attribute 'publish_step'`.

- [ ] **Step 3: Add the event + publisher.** In `src/autobot/core/events.py`, add the dataclass after `ChoicesEvent`:

```python
@dataclass(frozen=True, slots=True)
class StepEvent:
    """One tool step within a turn — for the chat drawer's live progress trace.

    ``status`` is ``"running"`` (emitted before the tool runs), then ``"done"`` or
    ``"failed"`` once the gate returns. ``index`` is the step's position in the
    current turn (0-based) so a client can update the same row in place.
    """

    index: int
    tool: str
    label: str
    status: str

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {
            "type": "step",
            "index": self.index,
            "tool": self.tool,
            "label": self.label,
            "status": self.status,
        }
```

and add this method to `EventBus` (next to `publish_choices`):

```python
    def publish_step(self, index: int, tool: str, label: str, status: str) -> None:
        """Broadcast a tool-step update (running/done/failed) for the chat trace."""
        self._emit(StepEvent(index, tool, label, status).message())
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `uv run pytest tests/unit/test_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/autobot/core/events.py tests/unit/test_events.py
git commit -s -m "feat(events): add a per-step progress event to the bus"
```

---

### Task 5: Emit steps from the orchestrator + per-step voice cue

**Files:**
- Modify: `src/autobot/orchestrator/state_machine.py`
- Modify: `src/autobot/app.py`
- Modify: `src/autobot/daemon/runner.py`
- Test: `tests/unit/test_state_machine.py`

**Interfaces:**
- Consumes: `EventBus.publish_step` (Task 4); existing `_ack_for`, `_format_ack`, `self._gate.ack_of`, `self._tts.speak`, `self._settings.speak_acknowledgements`.
- Produces: `Orchestrator(..., on_step: Callable[[int, str, str, str], None] | None = None)`; per-turn `self._step_index` and `self._last_spoken_ack`; methods `_emit_step(index, tool, label, status)` and `_step_label(call) -> str`. `build(..., on_step=...)` param.

- [ ] **Step 1: Write the failing tests.** Add to `tests/unit/test_state_machine.py` (the fakes `_FakeAudio`, `_FakeSTT`, `_ToolingLLM`, `_RecordingGate`, `_RecordingTTS`, and the `_orchestrator` helper already exist there):

```python
def test_execute_emits_running_then_done_step() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    steps: list[tuple[int, str, str, str]] = []
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("create a file"),
        llm=_ToolingLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        on_step=lambda i, tool, label, status: steps.append((i, tool, label, status)),
    )
    orch.run_once()
    # The single tool call emits a running step then a done step, same index.
    assert (0, "create_file", "Create file", "running") in steps
    assert (0, "create_file", "Create file", "done") in steps


def test_voice_cue_dedupes_repeated_phrases_within_a_turn() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate

    class _TwoToolLLM:
        def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
            execute(ToolCall(name="create_file", arguments={"path": "a"}))
            execute(ToolCall(name="create_file", arguments={"path": "b"}))
            return "done"

    tts = _RecordingTTS()
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice", speak_acknowledgements=True),
        audio=_FakeAudio(),
        stt=_FakeSTT("make two files"),
        llm=_TwoToolLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=tts,
    )
    orch.run_once()
    # Two identical-tool steps must not speak the same cue twice back-to-back.
    cues = tts.spoken[:-1]  # drop the final reply ("done")
    assert all(a != b for a, b in zip(cues, cues[1:]))
```

- [ ] **Step 2: Run them to verify they fail.**

Run: `uv run pytest tests/unit/test_state_machine.py::test_execute_emits_running_then_done_step tests/unit/test_state_machine.py::test_voice_cue_dedupes_repeated_phrases_within_a_turn -v`
Expected: FAIL — `Orchestrator.__init__` rejects `on_step`; dedupe not yet implemented.

- [ ] **Step 3: Add the `on_step` param and per-turn state.** In `Orchestrator.__init__`, add `on_step` to the signature immediately after the existing `on_context` parameter:

```python
        on_context: Callable[[dict[str, Any]], None] | None = None,
        on_step: Callable[[int, str, str, str], None] | None = None,
```

In the body, store it and add the per-turn counters (next to `self._acknowledged = False`):

```python
        self._on_step = on_step
        self._step_index = 0  # tool-step counter within the current turn
        self._last_spoken_ack = ""  # last per-step voice cue, to dedupe back-to-back repeats
```

- [ ] **Step 4: Rewrite `_execute` to emit steps and speak per step.** Replace the current `_execute` body with:

```python
    def _execute(self, call: ToolCall) -> ToolResult:
        """Executor handed to the LLM: mark EXECUTING, surface the step, run the gate."""
        self._sm.transition(State.EXECUTING)
        if call.name == "dismiss":
            self._dismissed = True
        index = self._step_index
        self._step_index += 1
        label = self._step_label(call)
        self._emit_step(index, call.name, label, "running")
        # Voice: a short cue per step so a multi-step chain isn't silent — deduped so
        # the same phrase isn't spoken back-to-back, and silent (ack="") tools say nothing.
        if self._settings.speak_acknowledgements and not self._text_mode:
            phrase = self._ack_for(call)
            if phrase and phrase != self._last_spoken_ack:
                self._last_spoken_ack = phrase
                self._tts.speak(phrase)
        result = self._gate.execute(call)
        self._emit_step(index, call.name, label, "done" if result.ok else "failed")
        self._transcript.tool(call.name, call.arguments, result.ok, result.content)
        return result

    def _emit_step(self, index: int, tool: str, label: str, status: str) -> None:
        """Publish a tool-step update to the UI, if a sink is wired. Never raises."""
        if self._on_step is None:
            return
        try:
            self._on_step(index, tool, label, status)
        except Exception:  # a UI hiccup must never break a turn
            _log.exception("on_step sink failed")

    def _step_label(self, call: ToolCall) -> str:
        """A short human label for a tool step: its ack phrasing, else a tidy name."""
        ack_of = getattr(self._gate, "ack_of", None)
        ack = ack_of(call.name) if callable(ack_of) else None
        if ack:  # the tool's own phrasing, e.g. "Opening {target}" -> "Opening Spotify"
            return _format_ack(ack, call.arguments).rstrip(".")
        return call.name.replace("_", " ").capitalize()
```

(Note: `self._acknowledged` is no longer read; leave its assignments in place — they're harmless — or remove them in this edit if you prefer. The dedupe via `_last_spoken_ack` replaces the once-per-turn gate.)

- [ ] **Step 5: Reset the per-turn counters at each turn start.** In `_process_voice_turn`, where `self._acknowledged = False` is set (just before `self._set_delivery("voice")`), add:

```python
        self._step_index = 0
        self._last_spoken_ack = ""
```

In `_run_text_turn_locked`, where `self._acknowledged = False` is set (after `self._sm.reset(State.PLANNING)`), add the same two lines.

- [ ] **Step 6: Run the orchestrator tests to verify they pass.**

Run: `uv run pytest tests/unit/test_state_machine.py -v`
Expected: PASS.

- [ ] **Step 7: Wire `on_step` through the composition root.** In `src/autobot/app.py`, add the param to `build(...)` (after `on_choices`):

```python
    on_choices: ChoicesSink | None = None,
    on_step: Callable[[int, str, str, str], None] | None = None,
```

and pass it into the `Orchestrator(...)` constructor (after `on_context=on_context,`):

```python
        on_context=on_context,
        on_step=on_step,
```

Add a one-line entry to `build`'s docstring Args, after the `on_choices` entry:

```
        on_step: Optional sink (index, tool, label, status) fed once per tool step
            (running, then done/failed), so the chat drawer can show a live step
            trace; the daemon wires it to the bus's ``publish_step``.
```

- [ ] **Step 8: Wire the daemon callback.** In `src/autobot/daemon/runner.py`, add a publisher next to `publish_context` / `publish_choices`:

```python
    def publish_step(index: int, tool: str, label: str, status: str) -> None:
        bus.publish_step(index, tool, label, status)
```

and pass it to `build(...)` (after `on_choices=publish_choices,`):

```python
        on_choices=publish_choices,
        on_step=publish_step,
```

- [ ] **Step 9: Verify the whole suite + types stay green.**

Run: `make check`
Expected: PASS.

- [ ] **Step 10: Commit.**

```bash
git add src/autobot/orchestrator/state_machine.py src/autobot/app.py src/autobot/daemon/runner.py tests/unit/test_state_machine.py
git commit -s -m "feat(orchestrator): surface each tool step (chat event + voice cue)"
```

---

### Task 6: Render the step trace in the chat drawer

**Files:**
- Modify: `ui/orb/chat.html`

**Interfaces:**
- Consumes: the `{"type":"step", index, tool, label, status}` WS frame (Task 4/5); existing `$()`, `#log` container, `hideTyping()`, the `.jack` bubble convention.
- Produces: a `renderStep(m)` function and a `.steptrace` element cleared when the reply arrives. No automated test — verify manually (steps below).

- [ ] **Step 1: Add the step-trace CSS.** In the `<style>` block (near the `.msg`/`.jack` rules around line 56), add:

```css
  .steptrace{align-self:flex-start;max-width:82%;display:flex;flex-direction:column;gap:4px;
    font-size:12px;color:var(--muted);padding:4px 2px;}
  .steptrace .row{display:flex;align-items:center;gap:7px;}
  .steptrace .dot{width:6px;height:6px;border-radius:50%;background:var(--muted);flex:none;}
  .steptrace .row.done .dot{background:#34c759;}
  .steptrace .row.failed .dot{background:var(--danger);}
  .steptrace .row.running .label{opacity:.9;} .steptrace .row.done .label,.steptrace .row.failed .label{opacity:.6;}
```

- [ ] **Step 2: Add the `renderStep` function.** In the `<script>` block (near `showChoices` / `renderContext`, before `connect()`), add:

```javascript
  // Live tool-step trace (type "step"): one row per step, updated in place by index.
  // Cleared when the assistant's reply bubble arrives (see clearSteps()).
  var stepTrace = null;
  function renderStep(m){
    if(!stepTrace){
      stepTrace = document.createElement("div");
      stepTrace.className = "steptrace";
      $("log").appendChild(stepTrace);
    }
    var row = stepTrace.querySelector('[data-i="' + m.index + '"]');
    if(!row){
      row = document.createElement("div");
      row.setAttribute("data-i", m.index);
      row.innerHTML = '<span class="dot"></span><span class="label"></span>';
      stepTrace.appendChild(row);
    }
    row.className = "row " + (m.status || "running");
    var suffix = m.status === "done" ? " ✓" : m.status === "failed" ? " ✗" : "…";
    row.querySelector(".label").textContent = (m.label || m.tool) + suffix;
    $("log").scrollTop = $("log").scrollHeight;
  }
  function clearSteps(){ if(stepTrace){ stepTrace.remove(); stepTrace = null; } }
```

- [ ] **Step 3: Route the event and clear on reply.** In the `ws.onmessage` handler (around line 565), add a branch:

```javascript
        else if(m.type === "step") renderStep(m);
```

Then, in `hideTyping()` (the function that runs when a reply lands / the turn ends), add `clearSteps();` as its first line so the trace collapses when the answer appears. (If `hideTyping` already clears UI, just add `clearSteps();` alongside.)

- [ ] **Step 4: Manual verification.** With Ollama running:

Run: `make run` (or launch the daemon + orb), open the chat drawer, and type a chaining request, e.g. *"list files in my Downloads and tell me how many there are"* or *"search the web for today's date and tell me"*.
Expected: a step trace appears ("Listing files…", "Searching the web…") with each row flipping to ✓/✗, then it disappears when Jack's reply bubble arrives. Confirm no console errors in the webview devtools.

- [ ] **Step 5: Commit.**

```bash
git add ui/orb/chat.html
git commit -s -m "feat(ui): show a live tool-step trace in the chat drawer"
```

---

## Self-review

**Spec coverage** (against [`docs/plans/autobot_multi_step_tools_plan.md`](../../plans/autobot_multi_step_tools_plan.md)):
- §3.1 local loop parity + anti-thrash + cap → **Task 1**. ✓
- §3.1 cloud force-final-answer → **Task 2**. ✓
- §3.3 prompt principle → **Task 3**. ✓
- §3.2 step event + bus → **Task 4**; emission at `_execute` + voice cue + wiring → **Task 5**; chat trace → **Task 6**. ✓
- §5 testing: new `test_ollama_llm.py` (Task 1), cloud cap test (Task 2), events test (Task 4), orchestrator emission + voice dedupe (Task 5). ✓
- §4 decisions: cap 8 (Global Constraints + Task 1/2), force-final (Tasks 1/2), voice deduped per-step cue (Task 5), Approach 1 (no protocol change — confirmed across tasks). ✓

**Placeholder scan:** no TBD/TODO; every code step shows complete code; the one no-automated-test task (Task 6, UI) has an explicit manual-verification step. ✓

**Type consistency:** `on_step` is `Callable[[int, str, str, str], None]` in `Orchestrator.__init__`, `build`, and `runner.publish_step`; `EventBus.publish_step(index, tool, label, status)` and `StepEvent(index, tool, label, status)` match; `_step_label`/`_emit_step`/`_final_answer_no_tools` signatures are consistent with their call sites. ✓
