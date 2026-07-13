"""Tests for the pure tool-call parsing helpers (no live Ollama needed)."""

from __future__ import annotations

import types
from typing import Any, cast

from autobot.core.types import ToolCall
from autobot.llm.ollama_llm import (
    estimate_tokens,
    message_content,
    needs_compaction,
    normalize_tool_calls,
    pick_context_length,
    render_messages,
    system_prompt,
    trim_history,
)


def test_system_prompt_is_mode_aware() -> None:
    voice = system_prompt("voice")
    chat = system_prompt("chat")
    # Voice: spoken, short, no markdown.
    assert "spoken aloud" in voice and "no lists" in voice.lower()
    # Chat: shown as text, light markdown allowed, not phrased as speech.
    assert "shown as text" in chat and "markdown" in chat.lower()
    assert "spoken aloud" not in chat
    # Shared principles stay in both (the base prompt).
    assert "You are Jack" in voice and "You are Jack" in chat


def test_system_prompt_teaches_multi_step_and_stopping() -> None:
    from autobot.llm.ollama_llm import system_prompt

    text = system_prompt("chat").lower()
    assert "several steps" in text  # encourages chaining within a turn
    assert "final answer" in text  # tells it to stop once it has enough
    assert "failed" in text  # don't repeat a call that already failed


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


def test_trim_history_starts_on_a_clean_user_turn() -> None:
    # A tail-slice could land mid tool-exchange; trim must skip leading
    # assistant/tool messages so a tool result never appears orphaned.
    hist: list[dict[str, Any]] = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]},
        {"role": "tool", "tool_name": "x", "content": "ok"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "done"},
    ]
    trimmed = trim_history(hist, 3)
    assert [m["role"] for m in trimmed] == ["user", "assistant"]


def test_ollama_persists_tool_exchange_in_history(tmp_path: Any) -> None:
    # The local model must keep the assistant tool call + tool result in history
    # (not just the final text), so a later "close it" can resolve what it opened.
    from autobot.agent.harness import AgentHarness
    from autobot.agent.session_store import SessionStore
    from autobot.config import Settings
    from autobot.core.types import ToolResult
    from autobot.llm.ollama_llm import OllamaLanguageModel
    from autobot.session_log import NullTranscript
    from autobot.tools.registry import ToolRegistry, ToolSpec

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="open_app",
            description="",
            parameters={"type": "object"},
            handler=lambda **k: "ok",
        )
    )
    tool_call = types.SimpleNamespace(
        function=types.SimpleNamespace(name="open_app", arguments={"name": "Safari"})
    )
    resp1 = types.SimpleNamespace(
        message=types.SimpleNamespace(content="", tool_calls=[tool_call]), prompt_eval_count=100
    )
    resp2 = types.SimpleNamespace(
        message=types.SimpleNamespace(content="Opened it.", tool_calls=[]), prompt_eval_count=120
    )

    class FakeClient:
        def __init__(self) -> None:
            self._responses = [resp1, resp2]

        def chat(self, **_kwargs: object) -> object:
            return self._responses.pop(0)

    m = object.__new__(OllamaLanguageModel)
    m._settings = Settings()
    m._registry = reg
    m._selector = None
    m._transcript = NullTranscript()
    m._memory = None
    m._client = cast(Any, FakeClient())
    m._last_prompt_tokens = 0
    m._context_tokens = 8192
    m._messages = []
    m._sent_start = 0
    m._user_msg = {}
    m._round_query = ""
    m._pinned = set()

    harness = AgentHarness(m, SessionStore(str(tmp_path)))
    harness.session.delivery_mode = "chat"
    reply = harness.run_turn(
        "open safari", lambda c: ToolResult(name=c.name, content="Opened Safari.")
    )
    assert reply == "Opened it."
    roles = [msg.get("role") for msg in harness.session.history]
    assert "tool" in roles  # the tool result is persisted, not dropped
    assert roles[0] == "user" and roles[-1] == "assistant"

    # Local parity for the context meter: a usage payload with the local model and no
    # cache billing (cache_read/write are None — the dev card hides those rows).
    usage = harness.context_usage()
    assert usage is not None
    assert usage["used"] == 120 and usage["window"] == 8192
    assert usage["model"] == m._settings.llm_model
    assert usage["cache_read"] is None and usage["cache_write"] is None
    # "This turn": prompt size in, generated tokens out (local has no cache, so in == used).
    assert usage["turn_in"] == 120 and usage["turn_out"] == 0


def test_ollama_new_session_clears_history_and_usage(tmp_path: Any) -> None:
    from autobot.agent.harness import AgentHarness
    from autobot.agent.session_store import SessionStore
    from autobot.config import Settings
    from autobot.llm.ollama_llm import OllamaLanguageModel
    from autobot.tools.registry import ToolRegistry

    m = OllamaLanguageModel(Settings(context_tokens=8192), ToolRegistry(), client=cast(Any, None))
    harness = AgentHarness(m, SessionStore(str(tmp_path)))
    harness.session.history = [{"role": "user", "content": "hi"}]
    harness.session.summary = "earlier stuff"
    harness.session.last_usage = {"used": 1200, "window": 8192}

    harness.new_session()

    assert harness.session.history == []
    assert harness.session.summary == ""
    assert harness.context_usage() is None  # meter reads empty until the next turn


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


def test_system_prompt_mentions_active_folder() -> None:
    from autobot.llm.ollama_llm import system_prompt

    assert "active folder" in system_prompt("chat").lower()


def test_active_folder_line_returns_path_when_policy_set(tmp_path: object) -> None:
    from pathlib import Path

    from autobot.llm.ollama_llm import active_folder_line
    from autobot.tools.access import AccessPolicy, set_active_policy

    tmp = Path(str(tmp_path))
    ws = tmp / "workspace"
    pol = AccessPolicy(tmp / "access.json", ws)
    set_active_policy(pol)
    try:
        line = active_folder_line()
        assert line.startswith("Active folder: ")
        assert str(ws.resolve()) in line
    finally:
        set_active_policy(None)


def test_active_folder_line_returns_empty_when_no_policy() -> None:
    from autobot.llm.ollama_llm import active_folder_line
    from autobot.tools.access import set_active_policy

    set_active_policy(None)
    assert active_folder_line() == ""


def test_meeting_state_line_empty_when_no_provider() -> None:
    from autobot.llm.ollama_llm import meeting_state_line
    from autobot.meeting.state import set_meeting_status_provider

    set_meeting_status_provider(None)
    assert meeting_state_line() == ""


def test_meeting_state_line_idle_when_not_recording() -> None:
    from autobot.llm.ollama_llm import meeting_state_line
    from autobot.meeting.state import set_meeting_status_provider

    set_meeting_status_provider(lambda: {"active": False, "state": "idle"})
    try:
        line = meeting_state_line()
        # Authoritative "not recording" so the model can't act on a stale "I'm recording".
        assert line and "no meeting" in line.lower()
    finally:
        set_meeting_status_provider(None)


def test_meeting_state_line_recording_when_active() -> None:
    from autobot.llm.ollama_llm import meeting_state_line
    from autobot.meeting.state import set_meeting_status_provider

    set_meeting_status_provider(
        lambda: {"active": True, "elapsed_s": 130.0, "paused": False, "state": "recording"}
    )
    try:
        line = meeting_state_line()
        assert "recording" in line.lower()
        assert "2 min" in line  # 130s → ~2 min elapsed
        assert "stop_meeting" in line  # points the model at the way to finish
    finally:
        set_meeting_status_provider(None)


def test_meeting_state_line_swallows_provider_errors() -> None:
    from autobot.llm.ollama_llm import meeting_state_line
    from autobot.meeting.state import set_meeting_status_provider

    def boom() -> dict[str, Any]:
        raise RuntimeError("recorder exploded")

    set_meeting_status_provider(boom)
    try:
        assert meeting_state_line() == ""  # a broken provider must never crash a turn
    finally:
        set_meeting_status_provider(None)


def test_ollama_assemble_injects_meeting_state() -> None:
    from autobot.agent.session import Session
    from autobot.config import Settings
    from autobot.llm.ollama_llm import OllamaLanguageModel
    from autobot.meeting.state import set_meeting_status_provider

    m = object.__new__(OllamaLanguageModel)
    m._settings = Settings()
    m._memory = None
    session = Session(id="t", cwd=".", model="m", delivery_mode="chat")

    set_meeting_status_provider(lambda: {"active": True, "elapsed_s": 60.0, "paused": False})
    try:
        msgs = m._assemble(session, {"role": "user", "content": "take minutes"})
        system_text = " ".join(str(x.get("content", "")) for x in msgs if x.get("role") == "system")
        assert "recording" in system_text.lower()
    finally:
        set_meeting_status_provider(None)
