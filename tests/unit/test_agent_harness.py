from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness
from autobot.agent.session import Session
from autobot.agent.session_store import SessionStore
from autobot.core.streaming import active_session_id
from autobot.core.types import ToolCall, ToolResult
from autobot.tasks import NotificationInbox


class FakeChatModel:
    """Scriptable ChatModel: returns queued ChatResponses; records interactions.

    ``finalize_turn`` simulates real-adapter compaction: it returns the messages
    this turn appended to ``session.history`` (as the protocol requires), but then
    shrinks ``session.history`` itself down to just its last entry — mirroring how
    a real adapter's post-turn compaction reassigns ``session.history`` to a
    SHORTER list. Any test relying on ``session.history[start:]`` after the turn
    would find those messages gone; only the harness persisting the *returned*
    messages survives this.
    """

    def __init__(self, responses: list[ChatResponse], *, final: str = "FINAL") -> None:
        self._responses = list(responses)
        self._final = final
        self.recorded: list[list[tuple[ToolCall, ToolResult]]] = []
        self.turns: list[str] = []
        self.finalized = 0

    def begin_turn(self, session: Session, user_text: str) -> None:
        self.turns.append(user_text)
        session.history.append({"role": "user", "content": user_text})

    def send(self, session: Session, on_event: Any = None) -> ChatResponse:
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

    def finalize_turn(self, session: Session) -> list[dict[str, Any]]:
        self.finalized += 1
        session.last_usage = {"used": 1}
        new = list(session.history)
        session.history = session.history[-1:]  # simulate compaction: shrink in place
        return new

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


def test_turn_persisted_even_when_finalize_turn_compacts_history(store: SessionStore) -> None:
    """Regression: a compaction/trim turn must not lose its messages from the transcript.

    ``FakeChatModel.finalize_turn`` simulates real-adapter compaction by shrinking
    ``session.history`` down to just its last entry. Before the fix, the harness
    persisted ``session.history[start:]`` — sliced from the ALREADY-SHRUNK history —
    which is empty (or wrong) on a compaction turn, silently dropping the turn from
    the JSONL transcript. The fix persists ``finalize_turn``'s *return value*
    instead, which is captured before the shrink.
    """
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)
    harness.run_turn("hi", _ok_executor)

    # In-memory session.history was compacted down to one entry by the fake.
    assert len(harness.session.history) == 1

    # But the transcript on disk must still contain this turn's messages.
    loaded = store.load(harness.session.id)
    assert loaded is not None
    contents = [m.get("content") for m in loaded.history]
    assert "hi" in contents
    assert "hello" in contents


def test_resume_swaps_to_a_stored_session(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)
    original_id = harness.session.id

    other = store.create("/elsewhere", "some-model")
    store.append(other, [{"role": "user", "content": "from another session"}])

    assert harness.resume(other.id) is True
    assert harness.session.id == other.id
    assert harness.session.id != original_id
    contents = [m.get("content") for m in harness.session.history]
    assert "from another session" in contents


def test_resume_unknown_id_returns_false_and_leaves_session_unchanged(
    store: SessionStore,
) -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)
    original = harness.session

    assert harness.resume("does-not-exist") is False
    assert harness.session is original


def test_pending_notifications_are_folded_into_the_next_turn(store: SessionStore) -> None:
    """A completed background task's note is prepended to the next turn, then drained."""
    model = FakeChatModel([ChatResponse(text="ok", tool_calls=[])])
    inbox = NotificationInbox()
    harness = AgentHarness(model, store, inbox=inbox)
    inbox.push(harness.session.id, "Background command task-1 finished OK (exit 0).")

    assert harness.run_turn("what's next?", _ok_executor) == "ok"
    folded = model.turns[0]
    assert "task-1 finished" in folded  # the note reached the model
    assert "what's next?" in folded  # the user's actual text is preserved
    assert inbox.pending(harness.session.id) == 0  # one-shot: not redelivered next turn


def test_no_inbox_leaves_user_text_unchanged(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="ok", tool_calls=[])])
    harness = AgentHarness(model, store)  # inbox defaults to None
    harness.run_turn("hello", _ok_executor)
    assert model.turns[0] == "hello"


def test_empty_inbox_leaves_user_text_unchanged(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="ok", tool_calls=[])])
    harness = AgentHarness(model, store, inbox=NotificationInbox())
    harness.run_turn("hello", _ok_executor)
    assert model.turns[0] == "hello"


def test_active_session_id_seam_is_set_during_tool_execution(store: SessionStore) -> None:
    """A tool (e.g. a backgrounded run_command) can read the running session's id."""
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="peek")]),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )
    seen: list[str] = []

    def exec_(call: ToolCall) -> ToolResult:
        seen.append(active_session_id.get())
        return ToolResult(name=call.name, content="ok", ok=True)

    harness = AgentHarness(model, store)
    harness.run_turn("go", exec_)
    assert seen == [harness.session.id]


def test_no_redactor_leaves_tool_result_content_unchanged(store: SessionStore) -> None:
    """Default behavior (no redactor) is preserved: content reaches the model as-is."""
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="get_secret")]),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )

    def exec_(call: ToolCall) -> ToolResult:
        return ToolResult(  # gitleaks:allow — synthetic fixture, not a real key
            name=call.name, content="AKIAIOSFODNN7EXAMPLE", ok=True
        )

    harness = AgentHarness(model, store)  # redact defaults to None
    assert harness.run_turn("go", exec_) == "done"
    recorded_content = model.recorded[0][0][1].content
    assert recorded_content == "AKIAIOSFODNN7EXAMPLE"  # gitleaks:allow — synthetic fixture


def test_redactor_scrubs_secrets_from_tool_results_before_model_sees_them(
    store: SessionStore,
) -> None:
    """With a redactor injected, secret-shaped tool output never reaches record_results."""
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="get_secret")]),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )

    def exec_(call: ToolCall) -> ToolResult:
        return ToolResult(  # gitleaks:allow — synthetic fixture, not a real key
            name=call.name, content="AKIAIOSFODNN7EXAMPLE", ok=True
        )

    def redact(text: str) -> str:
        return text.replace("AKIAIOSFODNN7EXAMPLE", "«redacted»")  # gitleaks:allow — synthetic

    harness = AgentHarness(model, store, redact=redact)
    assert harness.run_turn("go", exec_) == "done"
    recorded_content = model.recorded[0][0][1].content
    assert "«redacted»" in recorded_content
    assert "AKIAIOSFODNN7EXAMPLE" not in recorded_content  # gitleaks:allow — synthetic fixture
