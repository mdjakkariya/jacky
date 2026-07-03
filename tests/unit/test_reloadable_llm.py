"""Tests for the reloadable LLM proxy (live settings/key reload)."""

from __future__ import annotations

from autobot.core.types import ToolCall, ToolResult
from autobot.llm.reloadable import ReloadableLanguageModel


class FakeLLM:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def run_turn(self, user_text: str, execute: object) -> str:
        return self.tag

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return self.tag


class FakeHarness(FakeLLM):
    """A FakeLLM that also supports session resume/list, like AgentHarness."""

    def __init__(self, tag: str) -> None:
        super().__init__(tag)
        self.resumed: list[str] = []

    def resume(self, session_id: str) -> bool:
        self.resumed.append(session_id)
        return session_id == "known"

    def list_sessions(self) -> list[dict[str, object]]:
        return [{"id": "known", "cwd": ".", "model": self.tag}]


def _exec(_c: ToolCall) -> ToolResult:  # unused stub
    return ToolResult(name="x", content="")


def test_builds_eagerly_and_delegates() -> None:
    model = ReloadableLanguageModel(lambda: FakeLLM("v1"))
    assert model.run_turn("hi", _exec) == "v1"


def test_rebuilds_only_after_mark_dirty() -> None:
    versions = iter(["v1", "v2", "v3"])
    model = ReloadableLanguageModel(lambda: FakeLLM(next(versions)))
    assert model.run_turn("a", _exec) == "v1"  # eager build
    assert model.run_turn("b", _exec) == "v1"  # not dirty -> same instance
    model.mark_dirty()
    assert model.run_turn("c", _exec) == "v2"  # rebuilt once
    assert model.run_turn("d", _exec) == "v2"  # stays until next mark_dirty


def test_keeps_working_model_if_reload_fails() -> None:
    state = {"n": 0}

    def factory() -> FakeLLM:
        state["n"] += 1
        if state["n"] == 2:  # fail the reload, not the first build
            raise RuntimeError("bad key")
        return FakeLLM(f"v{state['n']}")

    model = ReloadableLanguageModel(factory)
    assert model.run_turn("a", _exec) == "v1"
    model.mark_dirty()
    assert model.run_turn("b", _exec) == "v1"  # reload failed -> kept v1


def test_resume_delegates_to_inner() -> None:
    inner = FakeHarness("v1")
    model = ReloadableLanguageModel(lambda: inner)
    assert model.resume("known") is True
    assert model.resume("unknown") is False
    assert inner.resumed == ["known", "unknown"]


def test_resume_is_false_when_inner_lacks_it() -> None:
    model = ReloadableLanguageModel(lambda: FakeLLM("v1"))
    assert model.resume("anything") is False


def test_list_sessions_delegates_to_inner() -> None:
    model = ReloadableLanguageModel(lambda: FakeHarness("v1"))
    assert model.list_sessions() == [{"id": "known", "cwd": ".", "model": "v1"}]


def test_list_sessions_is_empty_when_inner_lacks_it() -> None:
    model = ReloadableLanguageModel(lambda: FakeLLM("v1"))
    assert model.list_sessions() == []
