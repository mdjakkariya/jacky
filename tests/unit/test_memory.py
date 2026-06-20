"""Tests for the persistent memory store and the memory tools."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.memory.store import MemoryStore
from autobot.tools.memory import register_memory_tools
from autobot.tools.registry import ToolRegistry


def _store() -> MemoryStore:
    return MemoryStore(":memory:")


def test_name_round_trips() -> None:
    store = _store()
    assert store.get_name() is None
    store.set_name("  MD  ")
    assert store.get_name() == "MD"
    store.set_name("Sam")  # replaces
    assert store.get_name() == "Sam"


def test_facts_dedupe_case_insensitively() -> None:
    store = _store()
    assert store.add_fact("likes jazz") is True
    assert store.add_fact("Likes Jazz") is False  # dup
    assert store.add_fact("works at BrowserStack") is True
    assert store.facts() == ["likes jazz", "works at BrowserStack"]


def test_forget_removes_matching_facts() -> None:
    store = _store()
    store.add_fact("likes jazz")
    store.add_fact("likes hiking")
    store.add_fact("works at BrowserStack")
    assert store.forget("likes") == 2
    assert store.facts() == ["works at BrowserStack"]
    assert store.forget("nothing") == 0


def test_context_asks_for_name_when_unknown() -> None:
    # First meeting: no name -> nudge to introduce and ask, then save it.
    ctx = _store().context()
    assert "name" in ctx.lower() and "set_name" in ctx


def test_context_includes_name_and_facts_and_stops_asking() -> None:
    store = _store()
    store.set_name("MD")
    store.add_fact("likes jazz")
    ctx = store.context()
    assert "MD" in ctx and "likes jazz" in ctx
    assert "never recite it back" in ctx
    assert "set_name" not in ctx  # name known -> no longer asks for it


def test_persists_across_connections(tmp_path: object) -> None:
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "mem.db")
    s1 = MemoryStore(db)
    s1.set_name("MD")
    s1.add_fact("likes jazz")
    s1.close()
    s2 = MemoryStore(db)  # reopen — memory should survive
    assert s2.get_name() == "MD"
    assert s2.facts() == ["likes jazz"]


# --- tools ---------------------------------------------------------------
def test_memory_tools_write_to_store_and_are_write_risk() -> None:
    store = _store()
    registry = ToolRegistry()
    register_memory_tools(registry, store)

    for name in ("set_name", "remember", "forget"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.risk is Risk.WRITE

    assert registry.dispatch("set_name", {"name": "MD"}).ok
    assert store.get_name() == "MD"
    assert registry.dispatch("remember", {"fact": "likes jazz"}).ok
    assert "likes jazz" in store.facts()
    assert registry.dispatch("forget", {"topic": "jazz"}).ok
    assert store.facts() == []


def test_remember_reports_duplicate() -> None:
    store = _store()
    registry = ToolRegistry()
    register_memory_tools(registry, store)
    first = registry.dispatch("remember", {"fact": "likes jazz"}).content
    second = registry.dispatch("remember", {"fact": "likes jazz"}).content
    assert "remember that" in first.lower()
    assert "already" in second.lower()
