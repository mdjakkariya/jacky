# Sessions (Phase 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move conversation state out of the provider adapters into one `Session` object the `AgentHarness` owns, with workspace-scoped, resumable, JSONL-persisted sessions and per-session cost tracking.

**Architecture:** Today each `ChatModel` adapter (`OllamaLanguageModel`, `OpenAICompatibleModel`, `AnthropicLanguageModel`) owns its own `_history`, `_summary`, delivery mode, and usage/cost fields. This plan introduces a `Session` (id, cwd, model, provider-native `history`, `summary`, `delivery_mode`, `last_usage`, cost aggregates) that the `AgentHarness` holds and **threads through** the `ChatModel` turn primitives (`begin_turn(session, user_text)`, `send(session)`, `record_results(session, results)`, `handle_discovery(session, call)`, `final_answer_no_tools(session)`, `finalize_turn(session)`). Adapters become **stateless w.r.t. the conversation** — they read/append `session.history`/`session.summary`, write usage into `session.last_usage`, and keep only transient per-turn scratch (client, settings, registry, selector, and per-turn buffers valid only during one serialized turn). The harness serves `context_usage()`/`new_session()`/`set_delivery_mode()` directly from the Session (no model round-trip) and, after each turn, persists the new messages to a JSONL transcript via a `SessionStore` (list/load for resume). History stays provider-native, so a session is tied to its provider; **cross-provider resume is out of scope** (documented; a neutral-canonical history is a future enhancement).

**Tech Stack:** Python 3.11+, mypy strict, ruff, pytest (explicit fakes). No new deps (stdlib `json`, `uuid`, `pathlib`, `time`).

## Global Constraints

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- mypy strict — keep green. Full type hints. Google-style docstrings on public module/classes/functions (tests exempt). Line length 100; format via `uv run ruff format`.
- `Session` is a **mutable** `@dataclass(slots=True)` (it accumulates history/cost across a turn) — NOT frozen. Small value sub-objects (e.g. a cost record) may be frozen.
- The `ChatModel` protocol (Phase 1a, `src/autobot/agent/chat_model.py`) is being CHANGED: turn primitives gain a leading `session: Session` parameter. `complete(prompt, *, temperature)` stays session-less (one-shot). `context_usage`/`new_session`/`set_delivery_mode` are REMOVED from `ChatModel` (the harness serves them from the Session). Update the protocol, the harness, all three adapters, and their tests together so `make check` stays green.
- Behavior-preserving for a single live turn: assistant replies, compaction, tool-calling, and the context-meter payload shape must be unchanged from the user's perspective. This is a state-ownership move, not a behavior change.
- Provider-native history: Ollama/OpenAI messages are `{role, content, tool_calls?, tool_call_id?}`; Anthropic messages are `{role, content: <blocks>}`. The Session stores whatever shape its provider uses; adapters never see another provider's shape.
- Logging: `get_logger("session")` for the store/session lifecycle; seam events (session created, resumed, persisted), not per-message.
- Tools return strings, never raise out of dispatch; the executor/permission-gate seam is unchanged.
- Commit with Conventional Commits. **NO Co-Authored-By / AI-attribution trailer.** Stage EXPLICIT paths only (never `git add -A`/`.`/`-u`).
- `make check` (ruff + ruff-format + mypy strict + pytest) green before each task is done.
- Branch: continue on `feat/coding-agent-phase1`. Issue #46.

## File Structure

- Create `src/autobot/agent/session.py` — `Session` dataclass + `TurnUsage` cost record.
- Create `src/autobot/agent/session_store.py` — `SessionStore` (JSONL persist / list / load).
- Modify `src/autobot/agent/chat_model.py` — thread `session` through primitives; drop `context_usage`/`new_session`/`set_delivery_mode`.
- Modify `src/autobot/agent/harness.py` — own the `Session`, thread it, serve meter/new-session/delivery, persist on finalize.
- Modify `src/autobot/llm/ollama_llm.py` — de-state (conversation state → session).
- Modify `src/autobot/agent/providers/openai_compatible.py` — de-state.
- Modify `src/autobot/llm/anthropic_llm.py` — de-state (preserve cache/pairing on `session.history`).
- Modify `src/autobot/app.py::_build_llm` — pass a `SessionStore` to the harness.
- Modify `src/autobot/daemon/server.py` — session list/resume endpoints (thin).
- Tests: `tests/unit/test_session.py`, `tests/unit/test_session_store.py`, plus updates to `test_agent_harness.py`, the three adapter tests, `test_reloadable_llm.py`.

---

### Task 1: `Session` + `TurnUsage` + `SessionStore`

The data layer only — no adapter/harness wiring yet. This ships independently: a Session model and a store that round-trips it to JSONL.

**Files:**
- Create: `src/autobot/agent/session.py`
- Create: `src/autobot/agent/session_store.py`
- Test: `tests/unit/test_session.py`, `tests/unit/test_session_store.py`

**Interfaces:**
- Produces:
  - `Session(id: str, cwd: str, model: str, *, history=[], summary="", delivery_mode="voice", last_usage: dict|None = None, cost: TurnUsage = TurnUsage())` — mutable `@dataclass(slots=True)`.
  - `TurnUsage(in_tokens=0, out_tokens=0, usd=0.0, priced=False)` — mutable `@dataclass(slots=True)` running totals.
  - `SessionStore(root: str)` with `create(cwd, model) -> Session`, `append(session, events: list[dict]) -> None`, `load(session_id) -> Session | None`, `list() -> list[dict]` (id/cwd/model/updated summaries), `new_id() -> str`.

- [ ] **Step 1: Write the failing Session test**

Create `tests/unit/test_session.py`:

```python
from __future__ import annotations

from autobot.agent.session import Session, TurnUsage


def test_session_defaults() -> None:
    s = Session(id="s1", cwd="/tmp/proj", model="gpt-x")
    assert s.history == []
    assert s.summary == ""
    assert s.delivery_mode == "voice"
    assert s.last_usage is None
    assert s.cost.in_tokens == 0 and s.cost.usd == 0.0 and s.cost.priced is False


def test_session_history_is_mutable_and_independent() -> None:
    a = Session(id="a", cwd="/x", model="m")
    b = Session(id="b", cwd="/y", model="m")
    a.history.append({"role": "user", "content": "hi"})
    assert a.history and not b.history  # no shared default list


def test_turn_usage_accumulates() -> None:
    u = TurnUsage()
    u.in_tokens += 10
    u.out_tokens += 3
    u.usd += 0.01
    u.priced = True
    assert (u.in_tokens, u.out_tokens, round(u.usd, 2), u.priced) == (10, 3, 0.01, True)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.agent.session`.

- [ ] **Step 3: Implement `session.py`**

Create `src/autobot/agent/session.py`:

```python
"""The conversation state the AgentHarness owns and threads through a ChatModel.

A :class:`Session` holds everything that persists across turns — the
provider-native message history, the running summary, the delivery mode, and
usage/cost totals — so the provider adapters stay stateless per turn. History is
stored in whatever shape the session's provider uses (dict messages for
OpenAI/Ollama, content-block messages for Anthropic); a session is therefore
tied to its provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnUsage:
    """Running token/cost totals for a session (surfaced in the context meter)."""

    in_tokens: int = 0
    out_tokens: int = 0
    usd: float = 0.0
    priced: bool = False  # False = no list price known for this model (hide the $ row)


@dataclass(slots=True)
class Session:
    """One conversation: identity, working dir, model, and accumulated state."""

    id: str
    cwd: str
    model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    delivery_mode: str = "voice"  # "chat" (text) or else spoken
    last_usage: dict[str, Any] | None = None  # provider-shaped context-meter payload
    cost: TurnUsage = field(default_factory=TurnUsage)

    def reset(self) -> None:
        """Clear conversation + usage for a "new chat" (keeps id/cwd/model)."""
        self.history = []
        self.summary = ""
        self.last_usage = None
        self.cost = TurnUsage()
```

- [ ] **Step 4: Run the Session test (PASS)**

Run: `uv run pytest tests/unit/test_session.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Write the failing SessionStore test**

Create `tests/unit/test_session_store.py`:

```python
from __future__ import annotations

from pathlib import Path

from autobot.agent.session_store import SessionStore


def test_create_then_append_then_load_roundtrips(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/proj", model="gpt-x")
    assert s.id and s.cwd == "/proj" and s.model == "gpt-x"
    store.append(s, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}])
    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.id == s.id and loaded.cwd == "/proj" and loaded.model == "gpt-x"
    assert loaded.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
    ]


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert SessionStore(str(tmp_path)).load("nope") is None


def test_list_reports_sessions_most_recent_first(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    a = store.create(cwd="/a", model="m")
    store.append(a, [{"role": "user", "content": "1"}])
    b = store.create(cwd="/b", model="m")
    store.append(b, [{"role": "user", "content": "2"}])
    listed = store.list()
    ids = [row["id"] for row in listed]
    assert set(ids) == {a.id, b.id}
    assert all({"id", "cwd", "model"} <= row.keys() for row in listed)


def test_append_is_incremental_not_rewrite(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path))
    s = store.create(cwd="/p", model="m")
    store.append(s, [{"role": "user", "content": "one"}])
    store.append(s, [{"role": "assistant", "content": "two"}])
    loaded = store.load(s.id)
    assert loaded is not None
    assert [m["content"] for m in loaded.history] == ["one", "two"]
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/unit/test_session_store.py -v`
Expected: FAIL — `ModuleNotFoundError: autobot.agent.session_store`.

- [ ] **Step 7: Implement `session_store.py`**

Create `src/autobot/agent/session_store.py`:

```python
"""JSONL persistence + resume for :class:`~autobot.agent.session.Session`.

Each session is one newline-delimited JSON file under ``root/<id>.jsonl``. The
first line is a ``{"type": "meta", ...}`` header (id, cwd, model, created); every
later line is a ``{"type": "msg", "message": <provider-native message>}`` event,
appended as turns complete. This is append-only and diff-friendly, and a resume
just replays the ``msg`` lines back into ``Session.history``. Time is injected so
the logic is unit-testable and deterministic.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from autobot.agent.session import Session
from autobot.logging_setup import get_logger

_log = get_logger("session")


class SessionStore:
    """Creates, appends to, lists, and loads sessions as JSONL files."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        """A fresh session id (uuid4 hex)."""
        return uuid.uuid4().hex

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    def create(self, cwd: str, model: str) -> Session:
        """Create a new session and write its meta header line."""
        session = Session(id=self.new_id(), cwd=cwd, model=model)
        meta = {"type": "meta", "id": session.id, "cwd": cwd, "model": model}
        with self._path(session.id).open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta) + "\n")
        _log.info("session created id=%s cwd=%s model=%s", session.id, cwd, model)
        return session

    def append(self, session: Session, events: list[dict[str, Any]]) -> None:
        """Append message ``events`` (provider-native) to the session's transcript."""
        if not events:
            return
        path = self._path(session.id)
        if not path.exists():  # session created out-of-band; write a header first
            with path.open("w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"type": "meta", "id": session.id, "cwd": session.cwd, "model": session.model}
                    )
                    + "\n"
                )
        with path.open("a", encoding="utf-8") as fh:
            for msg in events:
                fh.write(json.dumps({"type": "msg", "message": msg}) + "\n")

    def load(self, session_id: str) -> Session | None:
        """Rebuild a session by replaying its transcript, or ``None`` if absent."""
        path = self._path(session_id)
        if not path.exists():
            return None
        meta: dict[str, Any] = {}
        history: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "meta":
                meta = rec
            elif rec.get("type") == "msg":
                history.append(rec["message"])
        session = Session(
            id=meta.get("id", session_id),
            cwd=meta.get("cwd", ""),
            model=meta.get("model", ""),
            history=history,
        )
        _log.info("session resumed id=%s messages=%d", session.id, len(history))
        return session

    def list(self) -> list[dict[str, Any]]:
        """Summaries of stored sessions (id/cwd/model/mtime), most recent first."""
        rows: list[dict[str, Any]] = []
        for path in self._root.glob("*.jsonl"):
            try:
                first = path.read_text(encoding="utf-8").splitlines()[0]
                meta = json.loads(first)
            except (OSError, IndexError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "id": meta.get("id", path.stem),
                    "cwd": meta.get("cwd", ""),
                    "model": meta.get("model", ""),
                    "mtime": path.stat().st_mtime,
                }
            )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return rows
```

- [ ] **Step 8: Run the store test (PASS) + typecheck**

Run: `uv run pytest tests/unit/test_session_store.py -v` (4 passed), then `uv run mypy src/autobot/agent/session.py src/autobot/agent/session_store.py`.
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add src/autobot/agent/session.py src/autobot/agent/session_store.py tests/unit/test_session.py tests/unit/test_session_store.py
git commit -m "feat(agent): add Session + SessionStore (JSONL, resumable) (#46)"
```

---

### Task 2: Thread `Session` through `ChatModel` + `AgentHarness`

Change the protocol so turn primitives take a `session`, drop the state-serving methods from `ChatModel`, and make the harness own the Session, serve the meter/new-session/delivery from it, and persist the transcript on finalize. Update the harness tests. (The three adapters are updated in Tasks 3-5; between Task 2 and Task 5 the adapters won't match the new protocol — that's expected within the branch and `make check` for THIS task focuses on the harness + Session; do not delete adapter code here.)

**IMPORTANT sequencing:** This task changes `chat_model.py` and `harness.py` and `test_agent_harness.py` only. It will make the THREE adapters fail typecheck (they still have the old signatures). To keep `make check` green *for this task*, this task ALSO applies the mechanical signature change to all three adapters' primitive defs (add the `session` param, delegate to the existing `self._`-based bodies via a temporary shim) — OR Tasks 2-5 are treated as one reviewable unit. To keep tasks independently green, do the following in THIS task: add the `session` parameter to each adapter primitive but keep the adapter's internal `self._history` etc. working by copying `session.history` in at `begin_turn` and back out at `finalize_turn` (a temporary bridge). Tasks 3-5 then remove the bridge per adapter. If that bridge is too fiddly, instead treat Tasks 2-5 as a single task (rename this "Task 2: protocol + harness + all three adapters") and land them in one commit. **Decision for the implementer: prefer the single-commit approach — do Task 2 through Task 5 as one commit** (the protocol change is atomic across all implementers of it). The task breakdown below (3/4/5) then becomes a checklist within this task rather than separate commits.

**Files:**
- Modify: `src/autobot/agent/chat_model.py`, `src/autobot/agent/harness.py`
- Test: `tests/unit/test_agent_harness.py`

**Interfaces:**
- `ChatModel` primitives become: `begin_turn(session: Session, user_text: str)`, `send(session: Session) -> ChatResponse`, `record_results(session: Session, results)`, `handle_discovery(session: Session, call) -> str | None`, `final_answer_no_tools(session: Session) -> str`, `finalize_turn(session: Session)`, `complete(prompt, *, temperature=0.0) -> str` (unchanged). REMOVE `context_usage`, `new_session`, `set_delivery_mode` from the protocol.
- `AgentHarness(model, store: SessionStore, *, max_rounds=8, doom_limit=4)`:
  - `session: Session` property (current session).
  - `run_turn(user_text, execute) -> str` — threads `self._session` through the primitives; after `finalize_turn`, persists the turn's new `history` slice via `store.append`.
  - `context_usage() -> dict | None` — returns `self._session.last_usage`.
  - `new_session() -> None` — `self._session = store.create(self._cwd, self._model)`.
  - `set_delivery_mode(mode) -> None` — `self._session.delivery_mode = mode`.
  - `resume(session_id) -> bool` — load via store; replace current session if found.
  - `complete(prompt, *, temperature=0.0)` — delegates to model (unchanged).

- [ ] **Step 1: Update the `ChatModel` protocol**

In `src/autobot/agent/chat_model.py`: import `Session` under `TYPE_CHECKING` (`from autobot.agent.session import Session`). Add `session: Session` as the first parameter to `begin_turn`, `send`, `record_results`, `handle_discovery`, `final_answer_no_tools`, `finalize_turn`. DELETE the `context_usage`, `new_session`, and `set_delivery_mode` methods from the protocol (they move to the harness). Leave `complete` unchanged. Update docstrings to mention the session.

- [ ] **Step 2: Update the harness tests first (RED)**

In `tests/unit/test_agent_harness.py`: update `FakeChatModel` so every primitive takes a leading `session` param and reads/writes `session.history`/`session.last_usage` (e.g., `send` appends an assistant msg to `session.history` and returns the queued `ChatResponse`; `finalize_turn` sets `session.last_usage = {"used": 1}`). Remove `context_usage`/`new_session`/`set_delivery_mode` from the fake. Construct the harness as `AgentHarness(model, store)` where `store` is a `SessionStore(str(tmp_path))` (add a `tmp_path` fixture). Update `test_delegation_methods_forward_to_model` → assert `harness.complete("p") == "ONESHOT"` and `harness.context_usage() == {"used": 1}` **after a turn** (since the harness now serves usage from the session the fake populated in `finalize_turn`). Add a test: after `run_turn`, `store.load(harness.session.id)` returns a session whose history contains the turn's messages (transcript persisted).

Run: `uv run pytest tests/unit/test_agent_harness.py -v`
Expected: FAIL (harness signature/behavior not updated yet).

- [ ] **Step 3: Rewrite `AgentHarness`**

Update `src/autobot/agent/harness.py`:
- Constructor: `__init__(self, model, store, *, cwd=".", model_name="", max_rounds=8, doom_limit=4)` — store the `SessionStore`, `cwd`, `model_name`; create the initial session: `self._session = store.create(cwd, model_name)`.
- `session` property returns `self._session`.
- `run_turn`: record `start = len(self._session.history)` before the loop; call `self._model.begin_turn(self._session, user_text)`; in the loop call `self._model.send(self._session)`, `self._model.handle_discovery(self._session, call)`, `self._model.record_results(self._session, results)`; on cap `self._model.final_answer_no_tools(self._session)`; after the loop `self._model.finalize_turn(self._session)`; then persist: `self._store.append(self._session, self._session.history[start:])`. Return the reply. (The doom/anti-thrash logic is unchanged.)
- `context_usage()` returns `self._session.last_usage`.
- `new_session()`: `self._session = self._store.create(self._session.cwd, self._session.model)`.
- `set_delivery_mode(mode)`: `self._session.delivery_mode = mode`.
- `resume(session_id) -> bool`: `loaded = self._store.load(session_id); ... self._session = loaded; return True` (else False).
- `complete` unchanged (delegates to model).

- [ ] **Step 4: Apply the primitive-signature change to all three adapters (de-state them)**

This is the bulk. For EACH of `src/autobot/llm/ollama_llm.py` (`OllamaLanguageModel`), `src/autobot/agent/providers/openai_compatible.py` (`OpenAICompatibleModel`), `src/autobot/llm/anthropic_llm.py` (`AnthropicLanguageModel`):
- Add `session: Session` as the first param to `begin_turn`/`send`/`record_results`/`handle_discovery`/`final_answer_no_tools`/`finalize_turn` (`from autobot.agent.session import Session` under TYPE_CHECKING).
- Replace `self._history` → `session.history`, `self._summary` → `session.summary`, and read the delivery mode from `session.delivery_mode` (drop `self._delivery_mode`/`set_delivery_mode`). Remove the now-unused instance fields (`self._history`, `self._summary`, `self._delivery_mode`, and the usage/cost fields listed below) from `__init__`.
- Usage/meter: instead of storing `self._last_prompt_tokens` etc., write the context-meter dict into `session.last_usage` at the end of `finalize_turn` (the SAME dict `context_usage()` used to return). For Anthropic, also update `session.cost` (in/out/usd/priced) in `finalize_turn` and include `price=session.cost.usd if session.cost.priced else None` in `session.last_usage` (preserving the current payload).
- DELETE each adapter's `context_usage`, `new_session`, `set_delivery_mode` methods (the harness owns these now). Keep `complete` (session-less). Keep transient per-turn buffers (e.g. Ollama's `_messages`/`_sent_start`/`_user_msg`, Anthropic's `_turn_start`/`_last_content`/counters, OpenAI's `_messages`/`_last_tool_calls`) — these are valid only during one serialized turn and may stay as instance scratch, but they must be RE-INITIALIZED in `begin_turn` from the passed `session` (e.g. Ollama's `begin_turn` builds `self._messages = self._assemble(session, user_msg)` where `_assemble` now reads `session.history`/`session.summary`/`session.delivery_mode`). Compaction helpers operate on `session.history`.
- The context window resolution (`_context_tokens`/`_window`) stays on the adapter (it's provider/model config, not conversation state).

Concretely, the transform per adapter is mechanical: every read/write of conversation state becomes a read/write of the passed `session`. Read each adapter's current code and apply it method by method. The `_assemble`/`_system` helpers gain a `session` parameter.

- [ ] **Step 5: Update the three adapter tests + reloadable test**

- `tests/unit/test_ollama_llm_adapter.py`, `tests/unit/test_openai_compatible.py`, `tests/unit/test_anthropic_llm_adapter.py`: every `m.begin_turn(...)`/`m.send()`/`m.record_results(...)` call now passes a `Session` (construct `from autobot.agent.session import Session; s = Session(id="t", cwd=".", model="m")` and thread it). Where a test asserted on `m.context_usage()`, assert on `s.last_usage` after `finalize_turn(s)` instead. Preserve every assertion's intent.
- `tests/unit/test_reloadable_llm.py`: `ReloadableLanguageModel` forwards `run_turn`/`complete`/`context_usage`/`new_session`/`set_delivery_mode` to the inner harness — all still present on `AgentHarness`, so this should pass unchanged. If the fake inner model in that test implemented the removed `ChatModel` methods, adjust the fake to the harness surface. Do NOT weaken assertions.
- Any `test_ollama_llm.py`/`test_anthropic_llm.py`/`test_llm_parsing.py`/`test_state_machine.py` case that drove a turn: it goes through `AgentHarness(model, store).run_turn(...)`, which now owns the session — update construction to pass a `SessionStore(tmp_path)` and drop any direct `context_usage`/`set_delivery_mode` calls on the adapter (call them on the harness).

- [ ] **Step 6: Run the full check**

Run: `make check`
Expected: green. Iterate on any adapter/test that still references removed fields. This is the integration crux — take it method by method.

- [ ] **Step 7: Commit (single atomic protocol+harness+adapters commit)**

```bash
git add src/autobot/agent/chat_model.py src/autobot/agent/harness.py src/autobot/llm/ollama_llm.py src/autobot/agent/providers/openai_compatible.py src/autobot/llm/anthropic_llm.py tests/unit/test_agent_harness.py tests/unit/test_ollama_llm_adapter.py tests/unit/test_openai_compatible.py tests/unit/test_anthropic_llm_adapter.py tests/unit/test_reloadable_llm.py
# plus any other test files you had to touch, staged explicitly
git commit -m "refactor(agent): move conversation state into Session; adapters stateless (#46)"
```

---

### Task 3: Wire `SessionStore` + resume into the composition root and daemon

**Files:**
- Modify: `src/autobot/app.py::_build_llm` (pass a `SessionStore`)
- Modify: `src/autobot/daemon/server.py` (session list/resume endpoints)
- Test: `tests/unit/test_build_llm_harness.py` (update), `tests/unit/test_daemon_server.py` (add list/resume)

**Interfaces:**
- `AgentHarness(model, store, cwd=..., model_name=...)` built in `_build_llm` with `store = SessionStore(settings.session_dir)`, `cwd` from the active `AccessPolicy` cwd (or `.`), `model_name` from the chosen provider's model.
- Daemon: `GET /sessions` → `store.list()`; `POST /sessions/resume {id}` → `harness.resume(id)`.

- [ ] **Step 1: Update `_build_llm` to construct the store and pass it**

In `src/autobot/app.py::_build_llm`, create `store = SessionStore(settings.session_dir)` once, and pass `store` (plus `cwd` and the provider's `model_name`) to each `AgentHarness(...)` construction (all three branches). Import `SessionStore` (function-local, matching convention). Update `tests/unit/test_build_llm_harness.py` and `test_build_llm_openai.py` constructions if the `AgentHarness` positional/keyword shape changed (they assert `isinstance(llm, AgentHarness)` and `llm._model` — still valid).

- [ ] **Step 2: Write failing daemon tests**

In `tests/unit/test_daemon_server.py`, add tests (following the file's existing FastAPI TestClient pattern): `GET /sessions` returns a list (possibly empty) of `{id, cwd, model}`; `POST /sessions/resume` with a known id returns success and with an unknown id returns a not-found/false result. Wire against a harness whose store has a created session. (Match the existing test harness/fixtures in the file — read them first.)

Run: `uv run pytest tests/unit/test_daemon_server.py -v` → FAIL (endpoints missing).

- [ ] **Step 3: Add the daemon endpoints**

In `src/autobot/daemon/server.py`, add thin endpoints that call through to the harness/store (mirroring how existing endpoints reach the orchestrator's llm). `GET /sessions` → the harness's `store.list()`; `POST /sessions/resume` → `harness.resume(id)`. Follow the exact wiring pattern the file uses to reach the LLM/orchestrator (read `/chat` and the `new_session`/context endpoints to see how the handle is obtained). Keep them thin (transport only).

- [ ] **Step 4: Full check + commit**

Run: `make check` (green).

```bash
git add src/autobot/app.py src/autobot/daemon/server.py tests/unit/test_build_llm_harness.py tests/unit/test_build_llm_openai.py tests/unit/test_daemon_server.py
git commit -m "feat(app): wire SessionStore + session list/resume into daemon (#46)"
```

---

## Self-Review

**1. Spec coverage (#46 — Sessions):**
- Workspace-scoped Session (id, cwd, model): Task 1. ✅
- Persistent JSONL transcripts + resume: Task 1 (store) + Task 2 (harness persists on finalize) + Task 3 (daemon resume). ✅
- Cost tracking moved out of the LLM classes: Task 2 (`TurnUsage` on Session; adapters write `session.last_usage`/`session.cost`). ✅
- "Full: move history/summary out of the adapters into Session" (user's chosen scope): Task 2 de-states all three adapters; the harness owns the Session. ✅
- Cross-provider resume: explicitly out of scope (history is provider-native) — documented in Architecture.

**2. Placeholder scan:** New files (session.py, session_store.py) have complete code. Task 2 Step 4 is a described mechanical transform (not verbatim code for all three adapters — they are large existing files being de-stated method-by-method; the implementer reads each and applies the stated rule). This is the one place the plan describes rather than transcribes, because the transform is a rename/move applied to ~600 lines across three files; the rule is precise ("every read/write of conversation state → the passed `session`; delete context_usage/new_session/set_delivery_mode; write the meter dict into session.last_usage in finalize_turn"). Flagged intentionally.

**3. Type consistency:** `Session`/`TurnUsage` field names are used identically across Task 1 (definition), Task 2 (harness + adapters write `session.history`/`.summary`/`.delivery_mode`/`.last_usage`/`.cost`), and Task 3. `AgentHarness(model, store, *, cwd, model_name, max_rounds, doom_limit)` matches its test usage. `SessionStore` method names (`create`/`append`/`load`/`list`/`new_id`) are consistent.

**Risk note for the executor:** Task 2 is large and atomic (protocol change ripples to all three adapters at once). Land it as one commit, keep `make check` as the gate, and de-state one adapter fully (Ollama) before starting the next so a failing typecheck localizes to the adapter in progress. The Anthropic adapter is the delicate one (cache breakpoints + tool_use/tool_result pairing now operate on `session.history` — a pure rename, but verify pairing integrity via its existing tests).

## Execution Handoff

After 1b, all of Phase 1 is complete (harness, any-LLM, sessions). Phase 2 (code tools + repo map + plan mode + checkpoints + security gate, issues #48-#53) is the next epic stage — the actual coding capability.
