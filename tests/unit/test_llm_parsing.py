"""Tests for the pure tool-call parsing helpers (no live Ollama needed)."""

from __future__ import annotations

import types

from autobot.core.types import ToolCall
from autobot.llm.ollama_llm import (
    estimate_tokens,
    message_content,
    needs_compaction,
    normalize_tool_calls,
    pick_context_length,
    render_messages,
    trim_history,
)


def test_normalize_dict_shaped_message() -> None:
    msg = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}],
    }
    assert normalize_tool_calls(msg) == [ToolCall(name="get_time", arguments={})]


def test_normalize_object_shaped_message() -> None:
    fn = types.SimpleNamespace(name="get_time", arguments={"tz": "local"})
    tc = types.SimpleNamespace(function=fn)
    msg = types.SimpleNamespace(tool_calls=[tc], content="")
    assert normalize_tool_calls(msg) == [ToolCall(name="get_time", arguments={"tz": "local"})]


def test_normalize_json_string_arguments() -> None:
    msg = {"tool_calls": [{"function": {"name": "get_time", "arguments": "{}"}}]}
    assert normalize_tool_calls(msg) == [ToolCall(name="get_time", arguments={})]


def test_normalize_bad_json_arguments_degrades_to_empty() -> None:
    msg: dict[str, object] = {"tool_calls": [{"function": {"name": "x", "arguments": "{not json"}}]}
    assert normalize_tool_calls(msg) == [ToolCall(name="x", arguments={})]


def test_normalize_skips_calls_without_a_name() -> None:
    msg: dict[str, object] = {"tool_calls": [{"function": {"arguments": {}}}]}
    assert normalize_tool_calls(msg) == []


def test_no_tool_calls_returns_empty() -> None:
    assert normalize_tool_calls({"content": "hi"}) == []


def test_message_content_strips_whitespace() -> None:
    assert message_content({"content": "  hello  "}) == "hello"
    assert message_content(None) == ""


def test_trim_history_keeps_most_recent() -> None:
    history = [{"role": "user", "content": str(i)} for i in range(8)]
    trimmed = trim_history(history, 4)
    assert [m["content"] for m in trimmed] == ["4", "5", "6", "7"]


def test_trim_history_under_limit_is_unchanged() -> None:
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert trim_history(history, 10) == history


def test_trim_history_zero_disables() -> None:
    assert trim_history([{"role": "user", "content": "a"}], 0) == []


def test_pick_context_length_finds_arch_specific_key() -> None:
    info = {"general.architecture": "qwen2", "qwen2.context_length": 32768, "x": 1}
    assert pick_context_length(info, 4096) == 32768


def test_pick_context_length_falls_back() -> None:
    assert pick_context_length(None, 4096) == 4096
    assert pick_context_length({"no.match": "x"}, 8192) == 8192


def test_needs_compaction_threshold() -> None:
    # 90% of 1000 = 900.
    assert needs_compaction(900, 1000, 0.9) is True
    assert needs_compaction(899, 1000, 0.9) is False
    assert needs_compaction(5000, 0, 0.9) is False  # unknown context -> never


def test_render_messages_flattens() -> None:
    out = render_messages(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    )
    assert out == "user: hi\nassistant: yo"


def test_estimate_tokens_from_chars() -> None:
    # 40 characters of content at ~4 chars/token -> ~10 tokens.
    msgs = [{"role": "user", "content": "x" * 40}]
    assert estimate_tokens(msgs, chars_per_token=4) == 10


def test_estimate_catches_a_large_incoming_message() -> None:
    # A big new message pushes the estimate over 85% of a small window, so the
    # proactive check would compact before sending (the edge case we fixed).
    history = [{"role": "assistant", "content": "a" * 100}]
    big_user_msg = {"role": "user", "content": "b" * 4000}
    est = estimate_tokens([*history, big_user_msg], chars_per_token=4)
    assert needs_compaction(est, context_tokens=1200, threshold=0.85) is True
