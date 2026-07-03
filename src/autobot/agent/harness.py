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
