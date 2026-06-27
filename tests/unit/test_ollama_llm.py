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
