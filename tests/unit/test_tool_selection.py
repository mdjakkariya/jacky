"""Tests for pure tool-selection logic (no model, no network)."""

from __future__ import annotations

from autobot.config import Settings
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.selection import (
    AllToolsSelector,
    LexicalToolSelector,
    build_tool_selector,
    score_tools,
    tokenize,
)


def _spec(name: str, desc: str = "", *, core: bool = False) -> ToolSpec:
    return ToolSpec(name=name, description=desc, parameters={}, handler=lambda: name, core=core)


def test_tokenize_lowercases_drops_short_and_stopwords() -> None:
    assert tokenize("What's MY battery?") == ["what", "battery"]
    assert tokenize("a to of the") == []


def test_score_tools_ranks_relevant_first_and_excludes_zero() -> None:
    battery = _spec("battery_status", "Check the Mac's battery level and charging state.")
    volume = _spec("set_volume", "Set the system output volume.")
    scored = score_tools("what's my battery", [battery, volume])
    assert [s.name for s, _ in scored] == ["battery_status"]  # volume scored 0 → excluded


def test_score_tools_empty_query_returns_empty() -> None:
    assert score_tools("", [_spec("x", "y")]) == []


def test_score_tools_name_match_beats_description_only() -> None:
    # "slack" in the name should outrank a tool that only mentions slack in prose.
    named = _spec("slack__send", "Send a message.")
    prose = _spec("notify", "Posts an update to a slack channel.")
    ranked = [s.name for s, _ in score_tools("send a slack message", [named, prose])]
    assert ranked[0] == "slack__send"


def test_all_tools_selector_returns_everything() -> None:
    reg = ToolRegistry()
    reg.register(_spec("a"))
    reg.register(_spec("b", core=True))
    selector = AllToolsSelector(reg)
    names = {s.name for s in selector.select("anything")}
    assert names == {"a", "b"}


# Task 4 tests: LexicalToolSelector + build_tool_selector


def _reg() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("set_volume", "Set the system output volume.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    reg.register(_spec("github__issue", "Create a GitHub issue."))
    return reg


def _lexical(reg: ToolRegistry, *, budget: int = 20) -> LexicalToolSelector:
    return LexicalToolSelector(reg, budget=budget, core_extra=frozenset(), core_remove=frozenset())


def test_core_tools_always_advertised() -> None:
    names = {s.name for s in _lexical(_reg()).select("what's my battery")}
    assert {"battery_status", "set_volume"} <= names


def test_gated_tool_appears_only_when_relevant() -> None:
    names = {s.name for s in _lexical(_reg()).select("send a slack message")}
    assert "slack__send" in names
    assert "github__issue" not in names  # irrelevant gated tool excluded


def test_irrelevant_query_advertises_core_only() -> None:
    names = {s.name for s in _lexical(_reg()).select("what's my battery")}
    assert names == {"battery_status", "set_volume"}  # no gated tool matched


def test_budget_caps_gated_additions_core_always_kept() -> None:
    # budget 2 == the 2 core tools → K=0, so a matching gated tool is still dropped.
    names = {s.name for s in _lexical(_reg(), budget=2).select("send a slack message")}
    assert names == {"battery_status", "set_volume"}


def test_pinned_tools_are_force_included() -> None:
    names = {s.name for s in _lexical(_reg()).select("hi", pinned=frozenset({"github__issue"}))}
    assert "github__issue" in names  # forced in despite zero relevance


def test_core_extra_and_remove_apply() -> None:
    reg = _reg()
    selector = LexicalToolSelector(
        reg, budget=20, core_extra=frozenset({"slack__send"}), core_remove=frozenset({"set_volume"})
    )
    names = {s.name for s in selector.select("hi")}
    assert "slack__send" in names  # promoted to core
    assert "set_volume" not in names  # demoted out of core


def test_build_tool_selector_picks_impl() -> None:
    reg = _reg()
    assert isinstance(build_tool_selector(Settings(tool_selection="all"), reg), AllToolsSelector)
    assert isinstance(
        build_tool_selector(Settings(tool_selection="lexical"), reg), LexicalToolSelector
    )
