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

    return OllamaLanguageModel(Settings(), ToolRegistry(), client=_FakeOllamaClient(message))


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
    m = _model({"content": "", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]})
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
