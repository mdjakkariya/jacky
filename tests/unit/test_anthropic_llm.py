"""Tests for the Anthropic backend — pure helpers + a turn with a fake client.

No network and no API key: a fake client returns canned responses, so the
tool-calling loop is exercised entirely offline.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.anthropic_llm import (
    AnthropicLanguageModel,
    _first_pairing_problem,
    cloud_error_reply,
    estimate_cost_usd,
    parse_tool_uses,
    text_from_content,
    to_anthropic_tools,
    trim_history,
    with_cache_breakpoint,
)
from autobot.tools.registry import ToolRegistry, ToolSpec


def _block(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def test_to_anthropic_tools_maps_input_schema() -> None:
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "open_app",
                "description": "Open",
                "parameters": {"type": "object"},
            },
        }
    ]
    out = to_anthropic_tools(schemas)
    assert out == [{"name": "open_app", "description": "Open", "input_schema": {"type": "object"}}]


def test_parse_tool_uses_and_text() -> None:
    content = [
        _block(type="text", text="Sure."),
        _block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"}),
    ]
    calls = parse_tool_uses(content)
    assert calls == [ToolCall(name="open_app", arguments={"name": "Safari"})]
    assert text_from_content(content) == "Sure."


class FakeMessages:
    """Returns queued responses; records the messages it was called with."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessages(responses)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="open_app",
            description="Open an app",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}},
            handler=lambda name: f"Opened {name}.",
        )
    )
    return reg


def test_run_turn_executes_tool_then_returns_final_text() -> None:
    # Round 1: model asks to call open_app. Round 2: model gives the final reply.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"})]
        ),
        SimpleNamespace(content=[_block(type="text", text="Opened Safari for you.")]),
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )

    executed: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call)
        return ToolResult(name=call.name, content="Opened Safari.")

    reply = model.run_turn("open safari", execute)
    assert reply == "Opened Safari for you."
    assert executed == [ToolCall(name="open_app", arguments={"name": "Safari"})]
    # Second API call carried the tool_result back to the model.
    second = model._client.messages.calls[1]
    assert any(
        isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
        for m in second["messages"]
        if isinstance(m.get("content"), list)
    )


def test_history_keeps_tool_blocks_across_turns() -> None:
    # Turn 1 opens a site (a tool round); turn 2 must see the *structured* record of
    # that tool call/result, not just text — so "close it" can resolve the target.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"})]
        ),
        SimpleNamespace(content=[_block(type="text", text="Opened it.")]),
        SimpleNamespace(content=[_block(type="text", text="Closed it.")]),
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    model.run_turn("open safari", lambda c: ToolResult(name=c.name, content="Opened Safari."))
    model.run_turn("close it", lambda c: ToolResult(name=c.name, content=""))

    # The 3rd API call (turn 2) carries the full prior turn: the tool_use AND its
    # tool_result are in the sent messages, so the model knows what it did.
    sent = model._client.messages.calls[2]["messages"]
    kinds = [
        _b.get("type")
        for m in sent
        if isinstance(m.get("content"), list)
        for _b in m["content"]
    ]
    assert "tool_use" in kinds and "tool_result" in kinds


def test_cache_breakpoint_is_on_the_last_block_only() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    out = with_cache_breakpoint(msgs)
    # Last block of the last message carries the breakpoint; earlier ones don't.
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in str(out[0])  # prefix stays clean/stable
    # The input wasn't mutated (history must stay byte-stable for caching).
    assert msgs[0]["content"] == "hi"


def test_pairing_problem_flags_unanswered_tool_use() -> None:
    bad = [
        {"role": "user", "content": "open safari"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": "close it"},  # never returned tool_result for t1
    ]
    assert _first_pairing_problem(bad) is not None and "t1" in _first_pairing_problem(bad)
    good = [
        {"role": "user", "content": "open safari"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert _first_pairing_problem(good) is None


def test_trim_history_starts_on_a_clean_user_turn() -> None:
    # A naive tail-slice could start on an orphaned tool_result; trim must skip to a
    # plain user turn so the API never sees a dangling tool exchange.
    hist = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "user", "content": "next thing"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    trimmed = trim_history(hist, 3)
    assert trimmed[0] == {"role": "user", "content": "next thing"}


def test_run_turn_no_tools_returns_text() -> None:
    responses = [SimpleNamespace(content=[_block(type="text", text="Hello there.")])]
    model = AnthropicLanguageModel(Settings(), _registry(), client=FakeClient(responses))
    assert model.run_turn("hi", lambda c: ToolResult(name=c.name, content="")) == "Hello there."


class BoomMessages:
    """A messages client whose create() always raises an API-style error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **_kwargs: Any) -> Any:
        raise self._exc


class BoomClient:
    def __init__(self, exc: Exception) -> None:
        self.messages = BoomMessages(exc)


def test_cloud_error_reply_is_calm_and_never_speaks_raw_api_text() -> None:
    # A noisy 404/limit body must NOT be read aloud — just a short, calm reply.
    err = RuntimeError("Error code: 404")
    err.body = {"error": {"message": "model: claude-3-5-haiku-latest"}}  # type: ignore[attr-defined]
    reply = cloud_error_reply(err)
    assert "isn't responding" in reply
    assert "try again" in reply and "Settings" in reply
    assert "404" not in reply and "claude-3-5-haiku" not in reply  # nothing raw spoken


def test_run_turn_returns_calm_reply_on_api_error() -> None:
    err = RuntimeError("Error code: 404")
    err.body = {"error": {"message": "model: claude-3-5-haiku-latest"}}  # type: ignore[attr-defined]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=BoomClient(err)
    )
    reply = model.run_turn("how are you", lambda c: ToolResult(name=c.name, content=""))
    assert "isn't responding" in reply
    assert "404" not in reply


def test_estimate_cost_usd_known_model() -> None:
    # Haiku 4.5 is $1/$5 per MTok: 1M in + 1M out = $1 + $5 = $6.
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_estimate_cost_usd_unknown_model_returns_none() -> None:
    assert estimate_cost_usd("some-future-model", 100, 100) is None


def test_run_turn_accumulates_token_usage() -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=120, output_tokens=8),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert model._session_in == 120
    assert model._session_out == 8


def test_system_prompt_includes_memory_when_present() -> None:
    class Mem:
        def context(self) -> str:
            return "What you know about the user: their name is MD."

    model = AnthropicLanguageModel(
        Settings(),
        _registry(),
        memory=Mem(),  # type: ignore[arg-type]
        client=FakeClient([]),
    )
    sys = model._system()
    assert "MD" in sys and "Autobot" in sys
