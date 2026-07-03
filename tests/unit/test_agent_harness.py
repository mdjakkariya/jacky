from __future__ import annotations

from pathlib import Path

import pytest

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness
from autobot.agent.session import Session
from autobot.agent.session_store import SessionStore
from autobot.core.types import ToolCall, ToolResult


class FakeChatModel:
    """Scriptable ChatModel: returns queued ChatResponses; records interactions."""

    def __init__(self, responses: list[ChatResponse], *, final: str = "FINAL") -> None:
        self._responses = list(responses)
        self._final = final
        self.recorded: list[list[tuple[ToolCall, ToolResult]]] = []
        self.turns: list[str] = []
        self.finalized = 0

    def begin_turn(self, session: Session, user_text: str) -> None:
        self.turns.append(user_text)
        session.history.append({"role": "user", "content": user_text})

    def send(self, session: Session) -> ChatResponse:
        resp = self._responses.pop(0)
        session.history.append({"role": "assistant", "content": resp.text})
        return resp

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        self.recorded.append(results)
        for call, result in results:
            session.history.append(
                {"role": "tool", "tool_name": call.name, "content": result.content}
            )

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        return None

    def final_answer_no_tools(self, session: Session) -> str:
        return self._final

    def finalize_turn(self, session: Session) -> None:
        self.finalized += 1
        session.last_usage = {"used": 1}

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return "ONESHOT"


def _ok_executor(call: ToolCall) -> ToolResult:
    return ToolResult(name=call.name, content=f"ran {call.name}", ok=True)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path))


def test_no_tool_calls_returns_reply_and_finalizes(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)
    assert harness.run_turn("hi", _ok_executor) == "hello"
    assert model.turns == ["hi"]
    assert model.finalized == 1


def test_one_tool_round_executes_then_replies(store: SessionStore) -> None:
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="get_time")]),
            ChatResponse(text="it is noon", tool_calls=[]),
        ]
    )
    seen: list[str] = []

    def exec_(call: ToolCall) -> ToolResult:
        seen.append(call.name)
        return ToolResult(name=call.name, content="noon", ok=True)

    harness = AgentHarness(model, store)
    assert harness.run_turn("time?", exec_) == "it is noon"
    assert seen == ["get_time"]
    assert model.recorded[0][0][1].content == "noon"


def test_repeated_failing_call_stops_with_failure_text(store: SessionStore) -> None:
    fail = ChatResponse(text="", tool_calls=[ToolCall(name="boom", arguments={"x": 1})])
    model = FakeChatModel([fail, fail])  # same call twice

    def exec_(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="it broke", ok=False)

    harness = AgentHarness(model, store)
    # round 1 executes (fails); round 2 re-issues the same call -> all_repeat -> stop.
    assert harness.run_turn("go", exec_) == "it broke"


def test_round_cap_forces_final_answer(store: SessionStore) -> None:
    # Every round asks for a *distinct* tool call so anti-thrash never trips; cap wins.
    responses = [
        ChatResponse(text="", tool_calls=[ToolCall(name="spin", arguments={"n": i})])
        for i in range(8)
    ]
    model = FakeChatModel(responses, final="gave up cleanly")
    harness = AgentHarness(model, store, max_rounds=8)
    assert harness.run_turn("go", _ok_executor) == "gave up cleanly"


def test_identical_call_repeated_trips_doom_loop_guard(store: SessionStore) -> None:
    def same() -> ChatResponse:
        return ChatResponse(text="", tool_calls=[ToolCall(name="p", arguments={"a": 1})])

    model = FakeChatModel([same(), same(), same(), same()], final="stopped")
    harness = AgentHarness(model, store, max_rounds=8, doom_limit=3)
    # succeeds each time (ok=True) so anti-thrash won't stop it; doom guard must.
    reply = harness.run_turn("go", _ok_executor)
    assert reply  # a non-empty explanation, not an infinite loop
    assert len(model.recorded) < 8  # stopped early


def test_delegation_methods_forward_to_model(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="x", tool_calls=[])])
    harness = AgentHarness(model, store)
    assert harness.complete("p") == "ONESHOT"
    harness.run_turn("hi", _ok_executor)
    # context_usage() is served from the session, which finalize_turn populated.
    assert harness.context_usage() == {"used": 1}


def test_turn_transcript_is_persisted_via_the_store(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)
    harness.run_turn("hi", _ok_executor)
    loaded = store.load(harness.session.id)
    assert loaded is not None
    contents = [m.get("content") for m in loaded.history]
    assert "hi" in contents and "hello" in contents
