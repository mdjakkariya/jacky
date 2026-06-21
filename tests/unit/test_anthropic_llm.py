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
    cloud_error_reply,
    parse_tool_uses,
    text_from_content,
    to_anthropic_tools,
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
