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
