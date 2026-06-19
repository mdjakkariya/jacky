"""Tests for the pure tool-call parsing helpers (no live Ollama needed)."""

from __future__ import annotations

import types

from autobot.core.types import ToolCall
from autobot.llm.ollama_llm import message_content, normalize_tool_calls


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
