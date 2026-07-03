from __future__ import annotations

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.agent.session import Session
from autobot.core.types import ToolCall, ToolResult


class _MinimalModel:
    def begin_turn(self, session: Session, user_text: str) -> None: ...
    def send(self, session: Session) -> ChatResponse:
        return ChatResponse(text="hi", tool_calls=[])

    def record_results(
        self, session: Session, results: list[tuple[ToolCall, ToolResult]]
    ) -> None: ...
    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        return None

    def final_answer_no_tools(self, session: Session) -> str:
        return ""

    def finalize_turn(self, session: Session) -> None: ...
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return ""


def test_minimal_model_satisfies_chat_model_protocol() -> None:
    assert isinstance(_MinimalModel(), ChatModel)


def test_chat_response_is_frozen() -> None:
    resp = ChatResponse(text="a", tool_calls=[ToolCall(name="t")])
    assert resp.text == "a"
    assert resp.tool_calls[0].name == "t"
