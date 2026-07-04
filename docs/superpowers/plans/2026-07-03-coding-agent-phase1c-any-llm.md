# Any LLM via API key (Phase 1c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user run Jack against any OpenAI-compatible LLM endpoint by supplying a base URL + model + API key, with keys stored via a cross-platform keyring.

**Architecture:** A new `OpenAICompatibleModel` implements the `ChatModel` protocol (from Phase 1a) against the `openai` SDK's `chat.completions` API — the lingua franca spoken by OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral, local vLLM/LM Studio, Gemini's OpenAI-compat endpoint, and Ollama's `/v1`. It mirrors `OllamaLanguageModel` (reusing its pure helpers for prompt assembly, trimming, and compaction), differing only in the SDK call, OpenAI response parsing, and `tool_call_id` pairing. `secrets.py` keeps its public API but swaps the macOS `security` CLI for the cross-platform `keyring` library. `config.py` gains a `"openai"` provider option (base URL + model + keyring-stored key), and `_build_llm` wraps it in an `AgentHarness` like the other providers.

**Tech Stack:** Python 3.11+, mypy strict, ruff, pytest (explicit fakes). New optional deps: `openai` (lazy-imported, `cloud` extra) and `keyring` (base dep — replaces the macOS-only Keychain shell-out).

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- mypy strict — keep it green. Full type hints. Google-style docstrings on public module/classes/functions (tests exempt). Line length 100; formatting via `uv run ruff format`.
- Value objects are `frozen=True, slots=True` dataclasses.
- The `ChatModel` protocol (Phase 1a, `src/autobot/agent/chat_model.py`) is: `begin_turn(user_text)`, `send() -> ChatResponse`, `record_results(list[tuple[ToolCall, ToolResult]])`, `handle_discovery(ToolCall) -> str | None`, `final_answer_no_tools() -> str`, `finalize_turn()`, `complete(prompt, *, temperature=0.0) -> str`, `context_usage() -> dict|None`, `new_session()`, `set_delivery_mode(mode)`. New adapters implement ALL of these. `_build_llm` returns `AgentHarness(model)`.
- Heavy SDKs (`openai`) are lazy-imported inside `__init__`/methods so the test suite stays import-light.
- On-device posture unchanged: using an OpenAI-compatible cloud endpoint is a disclosed, opt-in exception (same class as the Anthropic path); audio never leaves the device; keys live in the keyring, never on disk.
- Tools return strings and never raise out of dispatch; the executor seam (permission gate) is unchanged.
- Logging: `get_logger("provider")` for the adapter; log seam events (model selected, request failed), not per-token.
- Commit with Conventional Commits. **NO Co-Authored-By / AI-attribution trailer.** Stage EXPLICIT paths only (never `git add -A`/`.`/`-u`).
- `make check` (ruff + ruff-format + mypy strict + pytest) green before each task is done.
- Branch: continue on `feat/coding-agent-phase1` (per user). Issue #47.

## File Structure

- Modify `src/autobot/secrets.py` — swap the `security` CLI for `keyring`; keep the public API; injectable backend for tests.
- Modify `pyproject.toml` — add `keyring` (base) and `openai` (cloud extra).
- Create `src/autobot/agent/providers/__init__.py` — package marker.
- Create `src/autobot/agent/providers/openai_compatible.py` — `OpenAICompatibleModel` (ChatModel).
- Modify `src/autobot/config.py` — add `openai_base_url` + `openai_model` fields; `"openai"` is a valid `llm_provider`.
- Modify `src/autobot/app.py::_build_llm` — branch for `llm_provider == "openai"`.
- Tests: `tests/unit/test_secrets_keyring.py`, `tests/unit/test_openai_compatible.py`, `tests/unit/test_build_llm_openai.py`.

---

### Task 1: Cross-platform secrets via `keyring`

Replace the macOS `security` shell-out with the `keyring` library (Keychain on macOS, Credential Locker on Windows, Secret Service on Linux) while keeping `get_secret`/`set_secret`/`delete_secret`/`has_secret` signatures so no caller changes.

**Files:**
- Modify: `src/autobot/secrets.py`
- Modify: `pyproject.toml` (add `keyring>=25`)
- Test: `tests/unit/test_secrets_keyring.py`

**Interfaces:**
- Produces: `get_secret(name, backend=None) -> str | None`, `set_secret(name, value, backend=None) -> bool`, `delete_secret(name, backend=None) -> bool`, `has_secret(name, backend=None) -> bool`, where `backend` is an optional object with `get_password(service, name)`, `set_password(service, name, value)`, `delete_password(service, name)` (defaults to the `keyring` module). `_SERVICE = "autobot"` unchanged.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `keyring>=25` to the base `dependencies` list (it's pure-Python with platform backends). Run `uv sync` afterward.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_secrets_keyring.py`:

```python
from __future__ import annotations

from autobot import secrets


class _FakeKeyring:
    """In-memory keyring backend: (service, name) -> value."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        # keyring raises PasswordDeleteError when absent; mimic by KeyError
        del self.store[(service, name)]


def test_set_then_get_roundtrips() -> None:
    kr = _FakeKeyring()
    assert secrets.set_secret("anthropic_api_key", "sk-123", backend=kr) is True
    assert secrets.get_secret("anthropic_api_key", backend=kr) == "sk-123"


def test_get_missing_returns_none() -> None:
    assert secrets.get_secret("nope", backend=_FakeKeyring()) is None


def test_has_secret_reflects_presence() -> None:
    kr = _FakeKeyring()
    assert secrets.has_secret("k", backend=kr) is False
    secrets.set_secret("k", "v", backend=kr)
    assert secrets.has_secret("k", backend=kr) is True


def test_delete_removes_and_is_safe_when_absent() -> None:
    kr = _FakeKeyring()
    secrets.set_secret("k", "v", backend=kr)
    assert secrets.delete_secret("k", backend=kr) is True
    assert secrets.get_secret("k", backend=kr) is None
    # deleting again must not raise, returns False
    assert secrets.delete_secret("k", backend=kr) is False


def test_empty_value_is_treated_as_absent() -> None:
    kr = _FakeKeyring()
    secrets.set_secret("k", "", backend=kr)
    assert secrets.get_secret("k", backend=kr) is None
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/unit/test_secrets_keyring.py -v`
Expected: FAIL — `set_secret()` got an unexpected keyword `backend` (still the old `runner` API).

- [ ] **Step 4: Rewrite `secrets.py`**

Replace the body of `src/autobot/secrets.py` with the keyring-backed implementation (keep the module docstring's intent, update the mechanism):

```python
"""Cross-platform keyring-backed secret storage for API keys.

Secrets (e.g. an Anthropic / OpenAI / web-search API key) never touch
``settings.json`` or the logs — they live in the OS secret store via the
``keyring`` library: the login Keychain on macOS, Credential Locker on Windows,
and the Secret Service (libsecret) on Linux. All are stored under one service
name (``autobot``), keyed by an account name like ``anthropic_api_key``.

If no keyring backend is available (a headless Linux box with no Secret Service,
say), reads return ``None`` and writes fail gracefully, so the rest of the app
degrades cleanly. A ``backend`` is injectable so the logic is unit-tested without
touching a real keyring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    pass

_log = get_logger("app")

_SERVICE = "autobot"


class _Backend(Protocol):
    """The subset of the ``keyring`` API we use (so tests can inject a fake)."""

    def get_password(self, service: str, name: str) -> str | None: ...
    def set_password(self, service: str, name: str, value: str) -> None: ...
    def delete_password(self, service: str, name: str) -> None: ...


def _default_backend() -> _Backend:
    """The real ``keyring`` module (imported lazily so tests need no backend)."""
    import keyring

    return keyring


def get_secret(name: str, backend: _Backend | None = None) -> str | None:
    """Return the secret stored under ``name``, or ``None`` if not set/unavailable."""
    kr = backend or _default_backend()
    try:
        value = kr.get_password(_SERVICE, name)
    except Exception as exc:  # no backend, locked store, etc. — degrade cleanly
        _log.debug("keyring get failed for %s: %s", name, exc)
        return None
    return value or None


def set_secret(name: str, value: str, backend: _Backend | None = None) -> bool:
    """Store ``value`` under ``name`` (replacing any existing). Returns success."""
    kr = backend or _default_backend()
    try:
        kr.set_password(_SERVICE, name, value)
    except Exception as exc:
        _log.warning("keyring set failed for %s: %s", name, exc)
        return False
    return True


def delete_secret(name: str, backend: _Backend | None = None) -> bool:
    """Remove the secret stored under ``name``. Returns success (False if absent)."""
    kr = backend or _default_backend()
    try:
        kr.delete_password(_SERVICE, name)
    except Exception as exc:  # PasswordDeleteError when absent, or no backend
        _log.debug("keyring delete failed for %s: %s", name, exc)
        return False
    return True


def has_secret(name: str, backend: _Backend | None = None) -> bool:
    """Whether a secret is stored under ``name`` (without revealing it)."""
    return get_secret(name, backend) is not None
```

- [ ] **Step 5: Update existing callers/tests that used the old `runner=` param**

Run `grep -rn "runner=" src/autobot tests` and `grep -rn "get_secret\|set_secret\|delete_secret\|has_secret" src/autobot tests`. For each test that passed `runner=<fake>` (the old subprocess fake), switch it to `backend=_FakeKeyring()` (or delete the now-obsolete subprocess-runner test in the old `test_secrets*.py` if one exists and is fully superseded by `test_secrets_keyring.py`). Production callers (`anthropic_llm._require_key` → `get_secret("anthropic_api_key")`) pass no injectable and are unaffected. Do NOT weaken any assertion — only adapt the injection mechanism.

- [ ] **Step 6: Run tests + full check**

Run: `uv run pytest tests/unit/test_secrets_keyring.py -v` (PASS), then `make check`.
Expected: green. If an old `test_secrets.py` referenced the `security` CLI runner, it must now be updated or removed (superseded).

- [ ] **Step 7: Commit**

```bash
git add src/autobot/secrets.py pyproject.toml uv.lock tests/unit/test_secrets_keyring.py
# also stage any updated/removed old secrets test file explicitly
git commit -m "feat(secrets): store API keys via cross-platform keyring (#47)"
```

---

### Task 2: `OpenAICompatibleModel` ChatModel adapter

A `ChatModel` adapter for any OpenAI-compatible `chat.completions` endpoint. Mirrors `OllamaLanguageModel`, reusing its pure helpers; differs in the SDK call, response parsing, and `tool_call_id` pairing.

**Files:**
- Create: `src/autobot/agent/providers/__init__.py`
- Create: `src/autobot/agent/providers/openai_compatible.py`
- Modify: `pyproject.toml` (ensure `openai>=1.40` in the `cloud` extra)
- Test: `tests/unit/test_openai_compatible.py`

**Interfaces:**
- Consumes: `ChatResponse` (Phase 1a); `ToolCall`, `ToolResult` (`autobot.core.types`); reused pure helpers from `autobot.llm.ollama_llm`: `system_prompt`, `active_folder_line`, `meeting_state_line`, `trim_history`, `estimate_tokens`, `needs_compaction`, `render_messages`, `_HARD_MAX_MESSAGES`, `FIND_TOOLS` (from `autobot.tools.builtin`).
- Produces: `OpenAICompatibleModel(settings, registry, transcript=None, memory=None, client=None, selector=None)` implementing the full `ChatModel` protocol.

- [ ] **Step 1: Ensure the dependency**

In `pyproject.toml`, confirm/add `openai>=1.40` to the `[project.optional-dependencies].cloud` list (alongside `anthropic`). Run `uv sync --extra cloud`.

- [ ] **Step 2: Package marker**

Create `src/autobot/agent/providers/__init__.py`:

```python
"""Provider adapters implementing the ChatModel protocol."""

from __future__ import annotations
```

- [ ] **Step 3: Write the failing tests**

Create `tests/unit/test_openai_compatible.py`:

```python
from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.agent.providers.openai_compatible import OpenAICompatibleModel
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry


class _Msg:
    def __init__(self, content: str | None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _ToolCallObj:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Choice:
    def __init__(self, message: _Msg) -> None:
        self.message = message


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Resp:
    def __init__(self, message: _Msg) -> None:
        self.choices = [_Choice(message)]
        self.usage = _Usage(10, 4)


class _FakeCompletions:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.sent: list[list[dict[str, Any]]] = []

    def create(self, *, messages: list[dict[str, Any]], **kw: Any) -> _Resp:
        self.sent.append(messages)
        return self._resp


class _FakeOpenAI:
    def __init__(self, resp: _Resp) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(resp)})()


def _model(resp: _Resp) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=_FakeOpenAI(resp),
    )


def test_is_a_chat_model() -> None:
    assert isinstance(_model(_Resp(_Msg("hi"))), ChatModel)


def test_send_returns_text_when_no_tool_calls() -> None:
    m = _model(_Resp(_Msg("hello there", tool_calls=None)))
    m.begin_turn("hi")
    resp = m.send()
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello there"
    assert resp.tool_calls == []


def test_send_parses_tool_calls_with_json_arguments() -> None:
    tc = _ToolCallObj("call_1", "get_time", '{"tz": "utc"}')
    m = _model(_Resp(_Msg(None, tool_calls=[tc])))
    m.begin_turn("time?")
    resp = m.send()
    assert [c.name for c in resp.tool_calls] == ["get_time"]
    assert resp.tool_calls[0].arguments == {"tz": "utc"}


def test_record_results_appends_tool_message_with_tool_call_id() -> None:
    tc = _ToolCallObj("call_9", "get_time", "{}")
    client = _FakeOpenAI(_Resp(_Msg(None, tool_calls=[tc])))
    m = OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=client,
    )
    m.begin_turn("go")
    m.send()  # sets the last assistant tool_calls (id call_9)
    m.record_results([(ToolCall(name="get_time"), ToolResult(name="get_time", content="noon"))])
    m.send()
    last = client.chat.completions.sent[-1]
    tool_msgs = [x for x in last if x.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_9"
    assert tool_msgs[0]["content"] == "noon"


def test_bad_json_arguments_degrade_to_empty_dict() -> None:
    tc = _ToolCallObj("c", "t", "not json")
    m = _model(_Resp(_Msg(None, tool_calls=[tc])))
    m.begin_turn("x")
    resp = m.send()
    assert resp.tool_calls[0].arguments == {}
```

- [ ] **Step 4: Run to verify failure**

Run: `uv run pytest tests/unit/test_openai_compatible.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.agent.providers.openai_compatible`.

- [ ] **Step 5: Implement `openai_compatible.py`**

Create `src/autobot/agent/providers/openai_compatible.py`:

```python
"""OpenAI-compatible ``chat.completions`` adapter implementing :class:`ChatModel`.

Speaks the OpenAI Chat Completions dialect, which OpenAI, OpenRouter, Groq,
Together, DeepSeek, Mistral, local vLLM/LM Studio, Gemini's OpenAI-compat
endpoint, and Ollama's ``/v1`` all accept — so "any LLM via API key" is one
adapter parameterized by base URL + model + key. Structure mirrors
:class:`~autobot.llm.ollama_llm.OllamaLanguageModel` (same message shape); the
differences are the SDK call, response parsing, and ``tool_call_id`` pairing.

Using a cloud endpoint sends the conversation + tool schemas/results to that
provider — a disclosed, opt-in exception (like the Anthropic path). The key is
read from the keyring; audio never leaves the device.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from autobot.agent.chat_model import ChatResponse
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.ollama_llm import (
    active_folder_line,
    estimate_tokens,
    meeting_state_line,
    needs_compaction,
    render_messages,
    system_prompt,
    trim_history,
)
from autobot.logging_setup import get_logger
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.builtin import FIND_TOOLS
from autobot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from autobot.core.interfaces import ToolSelector
    from autobot.memory.store import MemoryStore

_log = get_logger("provider")

_DEFAULT_CONTEXT_TOKENS = 8192
_HARD_MAX_MESSAGES = 100
_SUMMARIZE_INSTRUCTION = (
    "Summarize the conversation so far in a few sentences. Preserve the user's goals, "
    "key facts, decisions, and any tool/web results. Be concise; this replaces older turns."
)


class OpenAICompatibleModel:
    """Runs turns against any OpenAI-compatible chat.completions endpoint."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        transcript: Transcript | None = None,
        memory: MemoryStore | None = None,
        client: Any | None = None,
        selector: ToolSelector | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._selector = selector
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        self._round_query = ""
        self._pinned: set[str] = set()
        self._history: list[dict[str, Any]] = []
        self._summary = ""
        self._delivery_mode = "voice"
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0
        self._context_tokens = settings.context_tokens or _DEFAULT_CONTEXT_TOKENS
        # Per-turn buffers shared by the ChatModel primitives.
        self._messages: list[dict[str, Any]] = []
        self._sent_start = 0
        self._user_msg: dict[str, Any] = {}
        self._last_tool_calls: list[dict[str, Any]] = []  # assistant tool_calls to pair results
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            from autobot.secrets import get_secret

            key = get_secret("openai_api_key") or "not-needed"  # local servers ignore the key
            self._client = OpenAI(base_url=settings.openai_base_url or None, api_key=key)
        _log.info(
            "openai-compatible ready base_url=%s model=%s",
            settings.openai_base_url or "(default)",
            settings.llm_model,
        )

    # --- prompt assembly (mirrors Ollama) ---
    def _assemble(self, user_msg: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(self._delivery_mode)}
        ]
        if self._memory is not None:
            profile = self._memory.context()
            if profile:
                messages.append({"role": "system", "content": profile})
        folder = active_folder_line()
        if folder:
            messages.append({"role": "system", "content": folder})
        meeting = meeting_state_line()
        if meeting:
            messages.append({"role": "system", "content": meeting})
        if self._summary:
            messages.append(
                {"role": "system", "content": f"Summary of earlier conversation: {self._summary}"}
            )
        messages += self._history
        messages.append(user_msg)
        return messages

    def _tools_for_round(self) -> list[dict[str, Any]]:
        if self._selector is None:
            return self._registry.schemas()
        selected = self._selector.select(self._round_query, pinned=frozenset(self._pinned))
        return [spec.to_schema() for spec in selected] + [FIND_TOOLS.to_schema()]

    def _discover_tools(self, intent: str) -> str:
        if self._selector is None:
            return "Tool discovery is unavailable."
        names = self._selector.search(intent)
        self._pinned.update(names)
        specs = [self._registry.get(n) for n in names]
        found = [s for s in specs if s is not None]
        if not found:
            return f"No tools found for: {intent}. Tell the user you can't do that."
        return "Found these tools (now available to call):\n" + "\n".join(
            f"- {s.name}: {s.description}" for s in found
        )

    # --- one model call ---
    def _create(self, messages: list[dict[str, Any]], *, with_tools: bool = True) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
        }
        if with_tools:
            tools = self._tools_for_round()
            if tools:
                kwargs["tools"] = tools
        resp = self._client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        self._last_prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        self._last_eval_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return resp

    @staticmethod
    def _parse(resp: Any) -> tuple[str, list[ToolCall], dict[str, Any]]:
        """Return (text, tool_calls, assistant_message_dict) from a completion."""
        choice = resp.choices[0]
        msg = choice.message
        text = (getattr(msg, "content", None) or "").strip()
        raw_calls = getattr(msg, "tool_calls", None) or []
        calls: list[ToolCall] = []
        recorded_calls: list[dict[str, Any]] = []
        for tc in raw_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            args_str = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(args_str)
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            call_id = getattr(tc, "id", None) or name
            calls.append(ToolCall(name=name, arguments=args))
            recorded_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                }
            )
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if recorded_calls:
            assistant["tool_calls"] = recorded_calls
        return text, calls, assistant

    # --- ChatModel primitives ---
    def begin_turn(self, user_text: str) -> None:
        """Start a turn: reset per-turn state, compact pre-flight, assemble messages."""
        self._user_msg = {"role": "user", "content": user_text}
        self._round_query = user_text
        self._pinned = set()
        estimated = estimate_tokens(self._assemble(self._user_msg))
        if needs_compaction(estimated, self._context_tokens, self._settings.compact_at):
            self._compact()
        self._messages = self._assemble(self._user_msg)
        self._sent_start = len(self._messages)
        self._last_tool_calls = []

    def send(self) -> ChatResponse:
        """Call the model once, record the assistant message, return text + tool calls."""
        resp = self._create(self._messages)
        text, calls, assistant = self._parse(resp)
        self._messages.append(assistant)
        self._last_tool_calls = assistant.get("tool_calls", [])
        return ChatResponse(text=text, tool_calls=calls)

    def handle_discovery(self, call: ToolCall) -> str | None:
        """Service a ``find_tools`` call inline; ``None`` for any normal tool call."""
        if call.name == FIND_TOOLS.name and self._selector is not None:
            return self._discover_tools(call.arguments.get("intent", ""))
        return None

    def record_results(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        """Append tool results, paired to the last assistant tool_calls' ids by order."""
        ids = [tc.get("id") for tc in self._last_tool_calls]
        for i, (call, result) in enumerate(results):
            tool_call_id = ids[i] if i < len(ids) else call.name
            self._messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": result.content}
            )

    def final_answer_no_tools(self) -> str:
        """One tools-disabled call to synthesize a reply when the round cap is hit."""
        _log.info("tool-round cap reached; forcing a final answer without tools")
        try:
            resp = self._create(self._messages, with_tools=False)
        except Exception:
            _log.exception("forced final answer failed")
            return "Sorry, that took too many steps."
        text, _calls, assistant = self._parse(resp)
        self._messages.append(assistant)
        return text or "Sorry, that took too many steps."

    def finalize_turn(self) -> None:
        """Persist this turn append-only, then post-turn compact + report usage."""
        self._history.extend([self._user_msg, *self._messages[self._sent_start :]])
        self._history = trim_history(self._history, _HARD_MAX_MESSAGES)
        if needs_compaction(self._last_prompt_tokens, self._context_tokens, self._settings.compact_at):
            self._compact()
        pct = round(100 * self._last_prompt_tokens / self._context_tokens) if self._context_tokens else 0
        _log.info("turn prompt_tokens=%d ctx=%d pct=%d", self._last_prompt_tokens, self._context_tokens, pct)

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion (no tools, no history)."""
        resp = self._client.chat.completions.create(
            model=self._settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return (getattr(resp.choices[0].message, "content", None) or "").strip()

    def context_usage(self) -> dict[str, Any] | None:
        """Context-meter payload, or None pre-turn (no prompt-cache billing here)."""
        if not self._last_prompt_tokens or not self._context_tokens:
            return None
        return {
            "used": self._last_prompt_tokens,
            "window": self._context_tokens,
            "cache_read": None,
            "cache_write": None,
            "turn_in": self._last_prompt_tokens,
            "turn_out": self._last_eval_tokens,
            "model": self._settings.llm_model,
        }

    def new_session(self) -> None:
        """Discard conversation history and start fresh."""
        self._history = []
        self._summary = ""
        self._last_prompt_tokens = 0
        self._last_eval_tokens = 0
        _log.info("session reset (new chat)")

    def set_delivery_mode(self, mode: str) -> None:
        """Set how the next reply is delivered ('chat' = text, else spoken)."""
        self._delivery_mode = mode

    # --- compaction (mirrors Ollama) ---
    def _compact(self) -> None:
        keep = self._settings.keep_recent_messages
        kept = trim_history(self._history, keep) if keep > 0 else []
        older = self._history[: len(self._history) - len(kept)]
        if not older:
            return
        body = (f"Previous summary: {self._summary}\n\n" if self._summary else "") + render_messages(older)
        try:
            resp = self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=[
                    {"role": "system", "content": _SUMMARIZE_INSTRUCTION},
                    {"role": "user", "content": body},
                ],
                temperature=0.0,
            )
            self._summary = (getattr(resp.choices[0].message, "content", None) or "").strip() or self._summary
        except Exception:
            _log.warning("summarization failed; keeping previous summary")
            return
        self._history = kept
        _log.info("compacted summarized=%d kept=%d", len(older), len(kept))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_openai_compatible.py -v`
Expected: PASS (5 passed).

- [ ] **Step 7: Typecheck + commit**

```bash
uv run mypy src/autobot/agent/providers/openai_compatible.py
git add src/autobot/agent/providers/__init__.py src/autobot/agent/providers/openai_compatible.py pyproject.toml uv.lock tests/unit/test_openai_compatible.py
git commit -m "feat(agent): add OpenAI-compatible ChatModel adapter (any LLM) (#47)"
```

---

### Task 3: Config fields + `_build_llm` wiring

Add the `"openai"` provider option and wire it in.

**Files:**
- Modify: `src/autobot/config.py`
- Modify: `src/autobot/app.py::_build_llm`
- Test: `tests/unit/test_build_llm_openai.py`

**Interfaces:**
- Consumes: `OpenAICompatibleModel` (Task 2), `AgentHarness` (Phase 1a).
- Produces: `Settings.openai_base_url: str`, `Settings.openai_model: str`; `_build_llm` returns `AgentHarness(OpenAICompatibleModel(...))` when `settings.llm_provider == "openai"`.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/unit/test_build_llm_openai.py`:

```python
from __future__ import annotations

from autobot.agent.harness import AgentHarness
from autobot.agent.providers.openai_compatible import OpenAICompatibleModel
from autobot.app import _build_llm
from autobot.config import Settings
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry


def test_build_llm_openai_returns_harness_wrapping_openai_model() -> None:
    settings = Settings(
        llm_provider="openai",
        openai_base_url="https://openrouter.ai/api/v1",
        llm_model="openai/gpt-4o-mini",
    )
    llm = _build_llm(settings, ToolRegistry(), NullTranscript(), None)
    assert isinstance(llm, AgentHarness)
    assert isinstance(llm._model, OpenAICompatibleModel)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_build_llm_openai.py -v`
Expected: FAIL — `Settings` has no `openai_base_url` (TypeError), or `_build_llm` falls through to Ollama.

- [ ] **Step 3: Add config fields**

In `src/autobot/config.py`, in the `# --- language model ---` block (after `ollama_host`, ~line 85), add:

```python
    # OpenAI-compatible provider ("openai"): any endpoint speaking chat.completions
    # (OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral, local vLLM/LM Studio,
    # Gemini's OpenAI-compat endpoint). The model id is llm_model; the key is stored
    # in the keyring under "openai_api_key". Blank base_url uses the SDK default (OpenAI).
    openai_base_url: str = ""
```

(The model id reuses the existing `llm_model` field, so no new model field is needed.) Update the `llm_provider` doc comment (line ~81-83) to mention `"openai"` as a third option.

- [ ] **Step 4: Wire `_build_llm`**

In `src/autobot/app.py::_build_llm`, before the final Ollama fallthrough (the `return AgentHarness(OllamaLanguageModel(...))` added in Phase 1a), add an `openai` branch:

```python
    if settings.llm_provider == "openai":
        from autobot.agent.providers.openai_compatible import OpenAICompatibleModel

        _log.info(
            "llm provider=openai base_url=%s model=%s (OFF-DEVICE)",
            settings.openai_base_url or "(default)",
            settings.llm_model,
        )
        selector = build_tool_selector(settings, registry)
        model = OpenAICompatibleModel(
            settings, registry, transcript, memory=memory, selector=selector
        )
        return AgentHarness(model)
```

Place it after the `if settings.llm_provider == "anthropic":` block and before the Ollama return, matching the existing structure (reuse the same `build_tool_selector(settings, registry)` call the Ollama branch uses — confirm its import/name in the file).

- [ ] **Step 5: Run the wiring test + full check**

Run: `uv run pytest tests/unit/test_build_llm_openai.py -v` (PASS), then `make check` (green).

- [ ] **Step 6: Commit**

```bash
git add src/autobot/config.py src/autobot/app.py tests/unit/test_build_llm_openai.py
git commit -m "feat(app): select the OpenAI-compatible provider via config (#47)"
```

---

## Self-Review

**1. Spec coverage (#47 — any LLM via API key):**
- OpenAI-compatible adapter covering the major providers + local + Gemini-via-compat: Task 2. ✅
- `Provider` config + selection: Task 3 (lightweight — `llm_provider="openai"` + `openai_base_url` + `llm_model`). A full multi-provider registry is intentionally deferred (YAGNI); noted here. ✅
- Cross-platform keyring (Keychain/Credential Locker/Secret Service): Task 1. ✅
- Native Gemini adapter: **deferred** — Gemini is reachable via its OpenAI-compatible endpoint through Task 2's adapter, so a separate adapter isn't needed for "any LLM via key." Noted.

**2. Placeholder scan:** every code step has complete code. Task 1 Step 5 and Task 2 Step 1 / Task 3 Step 4 contain grep/verify instructions with concrete follow-ups (update `runner=`→`backend=`; confirm `build_tool_selector` name), not vague TODOs. ✅

**3. Type consistency:** `ChatModel` primitive names/signatures match Phase 1a exactly and the Ollama adapter (Task 3 of Phase 1a). `_build_llm` returns `AgentHarness`; the test asserts `llm._model` (same white-box accessor pattern accepted in Phase 1a). `secrets` functions keep their names; `backend` replaces `runner`. ✅

---

## Execution Handoff

After 1c: **Phase 1b — Sessions (#46)** is the last Phase-1 plan (workspace-scoped sessions, JSONL transcripts, resume, cost tracking), then Phase 2 (code tools).
