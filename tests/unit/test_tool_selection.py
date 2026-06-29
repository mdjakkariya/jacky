"""Tests for pure tool-selection logic (no model, no network)."""

from __future__ import annotations

from collections.abc import Callable

from autobot.config import Settings
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.selection import (
    AllToolsSelector,
    EmbeddingToolSelector,
    LexicalToolSelector,
    _doc_key,
    build_tool_selector,
    cosine,
    embed_doc,
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


# Phase 2 tests: ToolSelector.search


def test_all_tools_search_ranks_by_relevance() -> None:
    reg = ToolRegistry()
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    reg.register(_spec("github__issue", "Create a GitHub issue."))
    names = AllToolsSelector(reg).search("send a slack message")
    assert names[0] == "slack__send"
    assert "github__issue" not in names  # scored 0 → excluded by score_tools


def test_lexical_search_returns_gated_names_excluding_core() -> None:
    # battery_status is core (always advertised) so search must never surface it,
    # even when the intent matches it.
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    names = _lexical(reg).search("send a slack message")
    assert names == ["slack__send"]


def test_lexical_search_excludes_core_even_when_intent_matches_core() -> None:
    reg = ToolRegistry()
    reg.register(_spec("battery_status", "Check the Mac's battery level.", core=True))
    reg.register(_spec("slack__send", "Send a message to a Slack channel."))
    assert _lexical(reg).search("what's my battery level") == []  # only core matched → no gated


def test_lexical_search_respects_core_extra_remove() -> None:
    reg = _reg()  # battery_status + set_volume core; slack__send + github__issue gated
    # Promote slack__send to core (so search hides it) and demote set_volume to gated.
    selector = LexicalToolSelector(
        reg,
        budget=20,
        core_extra=frozenset({"slack__send"}),
        core_remove=frozenset({"set_volume"}),
    )
    names = selector.search("send a slack message and set the volume")
    assert "slack__send" not in names  # promoted to core → excluded from search
    assert "set_volume" in names  # demoted to gated → now eligible


def test_search_honors_limit() -> None:
    reg = ToolRegistry()
    for i in range(5):
        reg.register(_spec(f"slack__send_{i}", "Send a message to a Slack channel."))
    names = _lexical(reg).search("send a slack message", limit=2)
    assert len(names) == 2


def test_search_empty_intent_returns_empty() -> None:
    assert _lexical(_reg()).search("") == []
    assert AllToolsSelector(_reg()).search("") == []


def test_cosine_identical_is_one_orthogonal_is_zero() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_handles_zero_vector_and_length_mismatch() -> None:
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector → no direction
    assert cosine([1.0], [1.0, 0.0]) == 0.0  # length mismatch → 0, never raises


def test_embed_doc_is_name_plus_description() -> None:
    assert embed_doc(_spec("battery_status", "Check the battery.")) == (
        "battery_status Check the battery."
    )


def test_doc_key_stable_for_same_text_changes_with_description() -> None:
    a = _doc_key(_spec("t", "one"))
    assert a == _doc_key(_spec("t", "one"))  # same name+desc → same key
    assert a != _doc_key(_spec("t", "two"))  # changed description → new key


def _fake_embedder(
    table: dict[str, list[float]], *, default: list[float] | None = None
) -> Callable[[str], list[float]]:
    """Deterministic embedder: maps a text to a fixed vector (substring-keyed)."""

    def embed(text: str) -> list[float]:
        for key, vec in table.items():
            if key in text:
                return vec
        if default is not None:
            return default
        raise AssertionError(f"no fake embedding for {text!r}")

    return embed


def _embedding(
    reg: ToolRegistry, embedder: Callable[[str], list[float]], *, budget: int = 20
) -> EmbeddingToolSelector:
    fallback = LexicalToolSelector(
        reg, budget=budget, core_extra=frozenset(), core_remove=frozenset()
    )
    return EmbeddingToolSelector(
        reg,
        embedder=embedder,
        fallback=fallback,
        budget=budget,
        core_extra=frozenset(),
        core_remove=frozenset(),
    )


def test_embedding_ranks_gated_by_cosine() -> None:
    # Query vector points at slack__send; github__issue is orthogonal → excluded by K? No —
    # K leaves room, but cosine 0 ranks it last; assert slack is chosen and ranked first.
    table = {
        "slack__send": [1.0, 0.0],
        "github__issue": [0.0, 1.0],
        "send a message via slack": [1.0, 0.0],  # query → slack direction
    }
    names = [
        s.name for s in _embedding(_reg(), _fake_embedder(table)).select("send a message via slack")
    ]
    assert "slack__send" in names
    assert names.index("slack__send") < names.index("github__issue")  # cosine ranks slack first


def test_embedding_core_always_advertised() -> None:
    table = {
        "battery_status": [1.0, 0.0],
        "set_volume": [0.0, 1.0],
        "slack__send": [0.0, 0.0],
        "github__issue": [0.0, 0.0],
    }
    names = {
        s.name
        for s in _embedding(_reg(), _fake_embedder(table, default=[0.0, 0.0])).select("anything")
    }
    assert {"battery_status", "set_volume"} <= names  # core always present


def test_embedding_budget_caps_gated_core_kept() -> None:
    table = {"slack__send": [1.0, 0.0]}
    names = {
        s.name
        for s in _embedding(_reg(), _fake_embedder(table, default=[1.0, 0.0]), budget=2).select(
            "send slack"
        )
    }
    assert names == {"battery_status", "set_volume"}  # budget 2 == core → K=0, no gated


def test_embedding_pinned_force_included() -> None:
    table = {"github__issue": [0.0, 1.0]}
    sel = _embedding(_reg(), _fake_embedder(table, default=[1.0, 0.0]))
    names = {s.name for s in sel.select("hi", pinned=frozenset({"github__issue"}))}
    assert "github__issue" in names  # forced in regardless of similarity


def test_embedding_search_returns_ranked_gated_names() -> None:
    table = {
        "slack__send": [1.0, 0.0],
        "github__issue": [0.0, 1.0],
        "post to a slack channel": [1.0, 0.0],
    }
    names = _embedding(_reg(), _fake_embedder(table, default=[0.0, 0.0])).search(
        "post to a slack channel", limit=1
    )
    assert names == ["slack__send"]  # best gated match only; core excluded from search


def test_embedding_caches_tool_vectors_embeds_once_per_tool() -> None:
    calls: list[str] = []

    def counting_embed(text: str) -> list[float]:
        calls.append(text)
        return [1.0, 0.0] if "slack" in text else [0.0, 1.0]

    sel = _embedding(_reg(), counting_embed)
    sel.select("send slack")
    sel.select("send slack")  # second call: tool vectors must be reused, not re-embedded
    tool_embeds = [c for c in calls if "Send a message" in c or "Create a GitHub" in c]
    # Two gated tools → embedded exactly once each across both select() calls.
    assert len(tool_embeds) == 2


def test_embedding_falls_back_to_lexical_on_embedder_error() -> None:
    def boom(text: str) -> list[float]:
        raise RuntimeError("ollama down / model not pulled")

    names = {s.name for s in _embedding(_reg(), boom).select("send a slack message")}
    assert "slack__send" in names  # lexical fallback found it despite the embedder failing
    assert "battery_status" in names  # core still advertised via the fallback path


def test_build_tool_selector_picks_embedding() -> None:
    sel = build_tool_selector(Settings(tool_selection="embedding"), _reg())
    assert isinstance(sel, EmbeddingToolSelector)


def test_build_tool_selector_embedding_has_lexical_fallback() -> None:
    sel = build_tool_selector(Settings(tool_selection="embedding"), _reg())
    assert isinstance(sel, EmbeddingToolSelector)
    assert isinstance(sel._fallback, LexicalToolSelector)
