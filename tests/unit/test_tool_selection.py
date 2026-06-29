"""Tests for pure tool-selection logic (no model, no network)."""

from __future__ import annotations

from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.selection import (
    AllToolsSelector,
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
