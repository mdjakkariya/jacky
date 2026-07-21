from __future__ import annotations

from pathlib import Path

import pytest

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness
from autobot.agent.session_store import SessionStore
from autobot.core.streaming import current_executor
from autobot.core.types import ToolCall, ToolResult

from .test_agent_harness import FakeChatModel


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path))


def test_current_executor_defaults_to_none() -> None:
    assert current_executor.get() is None


def test_current_executor_is_set_to_the_turn_executor_during_tool_execution(
    store: SessionStore,
) -> None:
    """A tool dispatched mid-turn (e.g. run_workflow) can retrieve the turn's executor."""
    model = FakeChatModel(
        [
            ChatResponse(text="", tool_calls=[ToolCall(name="run_workflow")]),
            ChatResponse(text="done", tool_calls=[]),
        ]
    )
    seen: list[object] = []

    def exec_(call: ToolCall) -> ToolResult:
        seen.append(current_executor.get())
        return ToolResult(name=call.name, content="ok", ok=True)

    harness = AgentHarness(model, store)
    assert harness.run_turn("go", exec_) == "done"
    assert seen == [exec_]  # the exact executor passed to run_turn


def test_current_executor_is_reset_after_the_turn(store: SessionStore) -> None:
    model = FakeChatModel([ChatResponse(text="hello", tool_calls=[])])
    harness = AgentHarness(model, store)

    def exec_(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="ok", ok=True)

    harness.run_turn("hi", exec_)
    assert current_executor.get() is None
