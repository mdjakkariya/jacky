"""The one agent loop, shared by every provider.

Extracted verbatim (behavior-preserving) from the duplicated ``run_turn`` loops
that used to live in ``llm/ollama_llm.py`` and ``llm/anthropic_llm.py``. The loop
drives a :class:`~autobot.agent.chat_model.ChatModel`: send → dispatch tool calls
through the injected executor (the permission gate) → feed results back → repeat,
until the model returns no tool calls (the final answer), a round only re-issues
already-failed calls, an identical call repeats past the doom-loop guard, or the
round cap is hit (then one tools-disabled call forces a final answer).

The harness owns the conversation :class:`~autobot.agent.session.Session` (not the
model): it creates the initial session, threads it through every primitive call,
and persists each turn's new messages via the injected
:class:`~autobot.agent.session_store.SessionStore` once the turn finalizes. This is
what keeps the provider adapters stateless across turns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from autobot.core.types import ToolCall, ToolResult
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.agent.chat_model import ChatModel
    from autobot.agent.session import Session
    from autobot.agent.session_store import SessionStore
    from autobot.core.types import ToolExecutor

_log = get_logger("harness")

_MAX_TOOL_ROUNDS = 8  # cap the plan→tool→result loop so it can't spin forever
_DOOM_LIMIT = 4  # abort if one identical (name+args) call repeats this many times
# Phase 2 TODO: when polling/wait tools arrive, key this on CONSECUTIVE identical
# calls with no intervening progress (not a per-turn total) so legitimate polling
# with identical args isn't cut off.


def _call_key(call: ToolCall) -> str:
    """Stable identity for a call (name + canonical args) for anti-thrash/doom checks."""
    return call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)


class AgentHarness:
    """Runs one user turn end-to-end against a :class:`ChatModel`, owning the Session."""

    def __init__(
        self,
        model: ChatModel,
        store: SessionStore,
        *,
        cwd: str = ".",
        model_name: str = "",
        max_rounds: int = _MAX_TOOL_ROUNDS,
        doom_limit: int = _DOOM_LIMIT,
    ) -> None:
        self._model = model
        self._store = store
        self._cwd = cwd
        self._model_name = model_name
        self._max_rounds = max_rounds
        self._doom_limit = doom_limit
        self._session = store.create(cwd, model_name)

    @property
    def session(self) -> Session:
        """The current conversation session."""
        return self._session

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user turn end-to-end; tool calls run through ``execute`` (the gate)."""
        start = len(self._session.history)
        self._model.begin_turn(self._session, user_text)
        failed: dict[str, str] = {}  # anti-thrash: call key -> failure text
        seen: dict[str, int] = {}  # doom-loop: call key -> times issued this turn
        reply = ""
        for _ in range(self._max_rounds):
            resp = self._model.send(self._session)
            if not resp.tool_calls:
                reply = resp.text
                break
            _log.info("planned tools=%s", [c.name for c in resp.tool_calls])
            results: list[tuple[ToolCall, ToolResult]] = []
            all_repeat = True  # did this round only re-issue calls that already failed?
            last_fail = ""
            doomed = False
            for call in resp.tool_calls:
                discovery = self._model.handle_discovery(self._session, call)
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
            self._model.record_results(self._session, results)
            if doomed:
                _log.info("stopping: identical tool call repeated past doom-loop guard")
                reply = "I kept trying the same step without progress, so I stopped."
                break
            if all_repeat:  # model is just retrying a failing step — stop and explain
                _log.info("stopping: round repeated only previously-failed tool calls")
                reply = last_fail or "I couldn't complete that, so I stopped."
                break
        else:
            reply = self._model.final_answer_no_tools(self._session)
        self._model.finalize_turn(self._session)
        self._store.append(self._session, self._session.history[start:])
        return reply

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion — delegated to the model (no tools, no loop)."""
        return self._model.complete(prompt, temperature=temperature)

    def context_usage(self) -> dict[str, Any] | None:
        """The current session's context-meter payload."""
        return self._session.last_usage

    def new_session(self) -> None:
        """Discard the current conversation and start a fresh session."""
        self._session = self._store.create(self._session.cwd, self._session.model)

    def set_delivery_mode(self, mode: str) -> None:
        """Set how the next reply is delivered ('chat' = text, else spoken)."""
        self._session.delivery_mode = mode

    def resume(self, session_id: str) -> bool:
        """Replace the current session with a stored one, or leave it unchanged.

        Returns:
            ``True`` if ``session_id`` was found and loaded, ``False`` otherwise.
        """
        loaded = self._store.load(session_id)
        if loaded is None:
            return False
        self._session = loaded
        return True
