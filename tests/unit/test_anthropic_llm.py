"""Tests for the Anthropic backend — pure helpers + a turn with a fake client.

No network and no API key: a fake client returns canned responses, so the
tool-calling loop is exercised entirely offline.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autobot.agent.harness import AgentHarness
from autobot.agent.session import Session
from autobot.agent.session_store import SessionStore
from autobot.config import Settings
from autobot.core.types import Risk, ToolCall, ToolResult
from autobot.llm.anthropic_llm import (
    TOOL_SEARCH_NAME,
    TOOL_SEARCH_TYPE,
    AnthropicLanguageModel,
    _first_pairing_problem,
    assemble_anthropic_tools,
    cloud_error_reply,
    estimate_cost_usd,
    is_too_long_error,
    parse_tool_uses,
    partition_tools,
    text_from_content,
    to_anthropic_tools,
    too_long_reply,
    tool_search_supported,
    trim_history,
    with_cache_breakpoint,
)
from autobot.tools.registry import ToolRegistry, ToolSpec


def _block(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _harness(model: AnthropicLanguageModel, tmp_path: Path) -> AgentHarness:
    return AgentHarness(model, SessionStore(str(tmp_path)))


def test_to_anthropic_tools_maps_input_schema() -> None:
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "open_app",
                "description": "Open",
                "parameters": {"type": "object"},
            },
        }
    ]
    out = to_anthropic_tools(schemas)
    assert out == [{"name": "open_app", "description": "Open", "input_schema": {"type": "object"}}]


def test_parse_tool_uses_and_text() -> None:
    content = [
        _block(type="text", text="Sure."),
        _block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"}),
    ]
    calls = parse_tool_uses(content)
    assert calls == [ToolCall(name="open_app", arguments={"name": "Safari"})]
    assert text_from_content(content) == "Sure."


class FakeMessages:
    """Returns queued responses; records the messages it was called with."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessages(responses)


class _FakeStream:
    """Mimics ``anthropic``'s ``MessageStream``: yields text deltas, then a final message."""

    def __init__(self, text_deltas: list[str], final_message: Any) -> None:
        self.text_stream = iter(text_deltas)
        self._final_message = final_message

    def get_final_message(self) -> Any:
        return self._final_message


class _FakeStreamManager:
    """Mimics ``anthropic``'s ``MessageStreamManager`` context manager returned by ``.stream()``."""

    def __init__(self, text_deltas: list[str], final_message: Any) -> None:
        self._text_deltas = text_deltas
        self._final_message = final_message

    def __enter__(self) -> _FakeStream:
        return _FakeStream(self._text_deltas, self._final_message)

    def __exit__(self, *_exc: Any) -> None:
        return None


class FakeStreamingMessages(FakeMessages):
    """Extends :class:`FakeMessages` with a fake ``.stream(**kwargs)`` entry point."""

    def __init__(self, responses: list[Any], text_deltas: list[str]) -> None:
        super().__init__(responses)
        self._text_deltas = text_deltas
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.stream_calls.append(kwargs)
        final_message = self._responses.pop(0)
        return _FakeStreamManager(self._text_deltas, final_message)


def _make_model_with_streaming_fake(
    *, text_deltas: list[str], final_text: str
) -> AnthropicLanguageModel:
    """A model whose fake client streams ``text_deltas`` then resolves to ``final_text``."""
    final_message = SimpleNamespace(
        content=[_block(type="text", text=final_text)],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    client = SimpleNamespace(messages=FakeStreamingMessages([final_message], text_deltas))
    return AnthropicLanguageModel(Settings(llm_provider="anthropic"), _registry(), client=client)


def _fresh_session() -> Session:
    return Session(id="t", cwd=".", model="m")


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="open_app",
            description="Open an app",
            parameters={"type": "object", "properties": {"name": {"type": "string"}}},
            handler=lambda name: f"Opened {name}.",
        )
    )
    return reg


def test_run_turn_executes_tool_then_returns_final_text(tmp_path: Path) -> None:
    # Round 1: model asks to call open_app. Round 2: model gives the final reply.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"})]
        ),
        SimpleNamespace(content=[_block(type="text", text="Opened Safari for you.")]),
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )

    executed: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call)
        return ToolResult(name=call.name, content="Opened Safari.")

    reply = _harness(model, tmp_path).run_turn("open safari", execute)
    assert reply == "Opened Safari for you."
    assert executed == [ToolCall(name="open_app", arguments={"name": "Safari"})]
    # Second API call carried the tool_result back to the model.
    second = model._client.messages.calls[1]
    assert any(
        isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
        for m in second["messages"]
        if isinstance(m.get("content"), list)
    )


def test_run_turn_stops_repeating_a_failing_tool_call(tmp_path: Path) -> None:
    # The model keeps asking for the same (failing) call; the loop must run it once and
    # then stop, surfacing the failure — not thrash to the round cap ("too many steps").
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id="t1", name="open_app", input={"name": "X"})]
        ),
        SimpleNamespace(
            content=[_block(type="tool_use", id="t2", name="open_app", input={"name": "X"})]
        ),
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    runs = {"n": 0}

    def execute(call: ToolCall) -> ToolResult:
        runs["n"] += 1
        return ToolResult(name=call.name, content="No access. Do NOT retry.", ok=False)

    reply = _harness(model, tmp_path).run_turn("open it", execute)
    assert runs["n"] == 1  # ran once; the identical repeat was short-circuited
    assert "do not retry" in reply.lower()


def test_history_keeps_tool_blocks_across_turns(tmp_path: Path) -> None:
    # Turn 1 opens a site (a tool round); turn 2 must see the *structured* record of
    # that tool call/result, not just text — so "close it" can resolve the target.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id="t1", name="open_app", input={"name": "Safari"})]
        ),
        SimpleNamespace(content=[_block(type="text", text="Opened it.")]),
        SimpleNamespace(content=[_block(type="text", text="Closed it.")]),
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("open safari", lambda c: ToolResult(name=c.name, content="Opened Safari."))
    harness.run_turn("close it", lambda c: ToolResult(name=c.name, content=""))

    # The 3rd API call (turn 2) carries the full prior turn: the tool_use AND its
    # tool_result are in the sent messages, so the model knows what it did.
    sent = model._client.messages.calls[2]["messages"]
    kinds = [
        _b.get("type") for m in sent if isinstance(m.get("content"), list) for _b in m["content"]
    ]
    assert "tool_use" in kinds and "tool_result" in kinds


def test_cache_breakpoint_is_on_the_last_block_only() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    out = with_cache_breakpoint(msgs)
    # Last block of the last message carries the breakpoint; earlier ones don't.
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in str(out[0])  # prefix stays clean/stable
    # The input wasn't mutated (history must stay byte-stable for caching).
    assert msgs[0]["content"] == "hi"


def test_pairing_problem_flags_unanswered_tool_use() -> None:
    bad: list[dict[str, Any]] = [
        {"role": "user", "content": "open safari"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
        {"role": "user", "content": "close it"},  # never returned tool_result for t1
    ]
    problem = _first_pairing_problem(bad)
    assert problem is not None and "t1" in problem
    good: list[dict[str, Any]] = [
        {"role": "user", "content": "open safari"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert _first_pairing_problem(good) is None


def test_trim_history_starts_on_a_clean_user_turn() -> None:
    # A naive tail-slice could start on an orphaned tool_result; trim must skip to a
    # plain user turn so the API never sees a dangling tool exchange.
    hist: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {"role": "user", "content": "next thing"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    trimmed = trim_history(hist, 3)
    assert trimmed[0] == {"role": "user", "content": "next thing"}


def test_is_too_long_error_matches_the_window_rejection() -> None:
    assert is_too_long_error(RuntimeError("prompt is too long: 201704 tokens > 200000 maximum"))
    assert not is_too_long_error(RuntimeError("Error code: 500 internal"))


class _TooLongThenOk:
    """Raises a 'prompt too long' error N times, then returns a normal response."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def create(self, **_kwargs: Any) -> Any:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("prompt is too long: 250000 tokens > 200000 maximum")
        return SimpleNamespace(
            content=[_block(type="text", text="ok now")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=3),
        )


def test_recovers_from_prompt_too_long_by_trimming_and_retrying(tmp_path: Path) -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    harness = _harness(model, tmp_path)
    for i in range(6):  # several old turns to trim away
        harness.session.history.append({"role": "user", "content": f"old {i}"})
        harness.session.history.append(
            {"role": "assistant", "content": [{"type": "text", "text": f"r{i}"}]}
        )
    msgs = _TooLongThenOk(fail_times=3)
    model._client = SimpleNamespace(messages=msgs)

    reply = harness.run_turn("new question", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok now"  # recovered, not the error reply
    assert msgs.calls == 4  # 3 rejections (each drops a turn) + 1 success
    assert len(harness.session.history) < 14  # oldest turns were trimmed away


def test_proactive_trim_drops_a_giant_old_turn_before_sending(tmp_path: Path) -> None:
    responses = [
        SimpleNamespace(
            content=[_block(type="text", text="hi")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    harness = _harness(model, tmp_path)
    harness.session.history.append({"role": "user", "content": "x" * 1_000_000})  # ~250k tokens
    harness.session.history.append(
        {"role": "assistant", "content": [{"type": "text", "text": "y"}]}
    )

    harness.run_turn("hello", lambda c: ToolResult(name=c.name, content=""))
    joined = "".join(str(m.get("content", "")) for m in harness.session.history)
    assert "x" * 1000 not in joined  # the giant turn was dropped to fit the window


def test_window_resolves_per_model_and_override() -> None:
    from autobot.llm.anthropic_llm import default_window_for, parse_window_limit

    assert default_window_for("claude-haiku-4-5") == 200_000
    assert default_window_for("some-unknown-future-model") == 200_000  # safe default
    too_long = RuntimeError("prompt is too long: 40000 tokens > 32768 maximum")
    assert parse_window_limit(too_long) == 32768
    assert parse_window_limit(RuntimeError("nope")) is None
    # Explicit settings override wins over the per-model default.
    m = AnthropicLanguageModel(
        Settings(anthropic_context_tokens=1_000_000), _registry(), client=FakeClient([])
    )
    assert m.context_window == 1_000_000


def test_window_resolved_live_from_models_api() -> None:
    # The Models API reports the real per-model limit, so a 1M model works with no
    # code change; FakeClient (no .models) falls back to the per-model default.
    class _Models:
        def retrieve(self, _model: str) -> Any:
            return SimpleNamespace(max_input_tokens=1_000_000)

    client = SimpleNamespace(messages=FakeMessages([]), models=_Models())
    m = AnthropicLanguageModel(Settings(llm_provider="anthropic"), _registry(), client=client)
    assert m.context_window == 1_000_000


def test_learns_smaller_window_from_error_then_fits(tmp_path: Path) -> None:
    # A model with a 32k window: first send is rejected; we learn 32768 from the
    # error, trim, and the window adapts dynamically (no hardcoded 200k).
    class _Small:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **_kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("prompt is too long: 50000 tokens > 32768 maximum")
            return SimpleNamespace(
                content=[_block(type="text", text="ok")],
                usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            )

    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    harness = _harness(model, tmp_path)
    for i in range(4):
        harness.session.history.append({"role": "user", "content": f"old {i}"})
        harness.session.history.append(
            {"role": "assistant", "content": [{"type": "text", "text": "r"}]}
        )
    model._client = SimpleNamespace(messages=_Small())
    reply = harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok"
    assert model.context_window == 32768  # learned from the rejection, not hardcoded


def test_compaction_summarizes_older_turns_and_keeps_recent(tmp_path: Path) -> None:
    # When a turn's prompt crosses compact_at, older turns are summarized (kept in the
    # system prompt) and the recent turns stay verbatim — instead of being dropped.
    turn = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            cache_read_input_tokens=180_000,
            cache_creation_input_tokens=0,
        ),
    )
    summ = SimpleNamespace(
        content=[_block(type="text", text="OLDER STUFF SUMMARY")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([turn, summ])
    )
    harness = _harness(model, tmp_path)
    for i in range(30):  # plenty of older turns to compact
        harness.session.history.append({"role": "user", "content": f"u{i}"})
        harness.session.history.append(
            {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}
        )

    reply = harness.run_turn("now", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok"
    assert harness.session.summary == "OLDER STUFF SUMMARY"  # older turns folded into a summary
    assert len(harness.session.history) <= 21  # only the recent tail kept verbatim
    assert "OLDER STUFF SUMMARY" in model._system(harness.session)  # summary in the system prompt


def test_new_session_clears_history_summary_and_usage(tmp_path: Path) -> None:
    # "New chat" must wipe the conversation and reset the context meter to empty.
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    harness = _harness(model, tmp_path)
    harness.session.history.append({"role": "user", "content": "hi"})
    harness.session.summary = "earlier stuff"
    harness.session.last_usage = {"used": 5000, "cache_read": 4000, "turn_in": 900}

    harness.new_session()

    assert harness.session.history == []
    assert harness.session.summary == ""
    assert harness.context_usage() is None  # meter reads empty until the next turn


def test_too_long_even_after_trim_returns_calm_reply_and_rolls_back(tmp_path: Path) -> None:
    class _AlwaysTooLong:
        def create(self, **_kwargs: Any) -> Any:
            raise RuntimeError("prompt is too long: 300000 tokens > 200000 maximum")

    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"),
        _registry(),
        client=SimpleNamespace(messages=_AlwaysTooLong()),
    )
    harness = _harness(model, tmp_path)
    reply = harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert reply == too_long_reply()
    assert harness.session.history == []  # the half-built turn was rolled back


def test_run_turn_no_tools_returns_text(tmp_path: Path) -> None:
    responses = [SimpleNamespace(content=[_block(type="text", text="Hello there.")])]
    model = AnthropicLanguageModel(Settings(), _registry(), client=FakeClient(responses))
    assert (
        _harness(model, tmp_path).run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
        == "Hello there."
    )


class BoomMessages:
    """A messages client whose create() always raises an API-style error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **_kwargs: Any) -> Any:
        raise self._exc


class BoomClient:
    def __init__(self, exc: Exception) -> None:
        self.messages = BoomMessages(exc)


def test_cloud_error_reply_is_calm_and_never_speaks_raw_api_text() -> None:
    # A noisy 404/limit body must NOT be read aloud — just a short, calm reply.
    err = RuntimeError("Error code: 404")
    err.body = {"error": {"message": "model: claude-3-5-haiku-latest"}}  # type: ignore[attr-defined]
    reply = cloud_error_reply(err)
    assert "isn't responding" in reply
    assert "try again" in reply and "Settings" in reply
    assert "404" not in reply and "claude-3-5-haiku" not in reply  # nothing raw spoken


def test_run_turn_returns_calm_reply_on_api_error(tmp_path: Path) -> None:
    err = RuntimeError("Error code: 404")
    err.body = {"error": {"message": "model: claude-3-5-haiku-latest"}}  # type: ignore[attr-defined]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=BoomClient(err)
    )
    reply = _harness(model, tmp_path).run_turn(
        "how are you", lambda c: ToolResult(name=c.name, content="")
    )
    assert "isn't responding" in reply
    assert "404" not in reply


def test_estimate_cost_usd_known_model() -> None:
    # Haiku 4.5 is $1/$5 per MTok: 1M in + 1M out = $1 + $5 = $6.
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_estimate_cost_usd_unknown_model_returns_none() -> None:
    assert estimate_cost_usd("some-future-model", 100, 100) is None


def test_estimate_cost_usd_includes_cache_pricing() -> None:
    # Cache tokens are billed on the INPUT rate: write = 1.25x, read = 0.1x. With Haiku
    # ($1 in / $5 out per MTok), 1M of each component: fresh in $1 + out $5 + write $1.25
    # + read $0.10 = $7.35. Omitting cache made an all-tools-cached prefix look free.
    cost = estimate_cost_usd(
        "claude-haiku-4-5",
        1_000_000,
        1_000_000,
        cache_read=1_000_000,
        cache_write=1_000_000,
    )
    assert cost == pytest.approx(7.35)


def test_estimate_cost_usd_cache_defaults_to_zero() -> None:
    # Back-compat: callers that pass no cache args price only fresh input + output.
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_estimate_cost_usd_unknown_model_ignores_cache() -> None:
    assert estimate_cost_usd("future", 100, 100, cache_read=100, cache_write=100) is None


def test_estimate_cost_usd_prices_sonnet_and_opus() -> None:
    # Pricing is keyed by model-id prefix, so Sonnet/Opus (and point releases) are priced
    # instead of showing "n/a". Sonnet $3/$15, Opus $5/$25 per MTok.
    assert estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    assert estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0


def test_context_usage_reports_session_price_for_priced_model(tmp_path: Path) -> None:
    # Default model (claude-haiku-4-5) is in the pricing table: 1M in @ $1 + 1M out @ $5 = $6.
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    usage = harness.context_usage()
    assert usage is not None
    assert usage["price"] == 6.0


def test_context_usage_price_is_none_for_unpriced_model(tmp_path: Path) -> None:
    # An unknown model has no list price: report None (the UI hides the row) rather
    # than a misleading $0.00.
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=8),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="some-future-model"),
        _registry(),
        client=FakeClient([resp]),
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    usage = harness.context_usage()
    assert usage is not None
    assert usage["price"] is None


def test_session_price_resets_on_new_session(tmp_path: Path) -> None:
    # "Price of the current session" must start fresh on New chat, not carry over.
    def _resp() -> SimpleNamespace:
        return SimpleNamespace(
            content=[_block(type="text", text="Hi.")],
            usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([_resp(), _resp()])
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    harness.new_session()
    harness.run_turn("hi again", lambda c: ToolResult(name=c.name, content=""))
    usage = harness.context_usage()
    assert usage is not None
    assert usage["price"] == 6.0  # one turn's cost, not two accumulated


def test_run_turn_accumulates_token_usage(tmp_path: Path) -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=120, output_tokens=8),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert harness.session.cost.in_tokens == 120
    assert harness.session.cost.out_tokens == 8


def test_system_prompt_includes_memory_when_present() -> None:
    class Mem:
        def context(self) -> str:
            return "What you know about the user: their name is MD."

    model = AnthropicLanguageModel(
        Settings(),
        _registry(),
        memory=Mem(),  # type: ignore[arg-type]
        client=FakeClient([]),
    )
    sys = model._system(Session(id="t", cwd=".", model="m"))
    assert "MD" in sys and "Jack" in sys


def test_run_turn_forces_final_answer_at_round_cap(tmp_path: Path) -> None:
    # 8 rounds each request a (distinct) tool and never finish; at the cap a final
    # tools-disabled call synthesizes the reply, not the canned "too many steps" line.
    responses = [
        SimpleNamespace(
            content=[_block(type="tool_use", id=f"t{i}", name="open_app", input={"name": f"X{i}"})],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )
        for i in range(8)
    ]
    responses.append(SimpleNamespace(content=[_block(type="text", text="Here's what I managed.")]))
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    reply = _harness(model, tmp_path).run_turn(
        "loop", lambda c: ToolResult(name=c.name, content="ok", ok=True)
    )
    assert reply == "Here's what I managed."  # forced final answer, not the canned line
    # The 9th (final) create was made with no tools.
    assert "tools" not in model._client.messages.calls[-1]


def _spec(name: str, *, core: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"desc for {name}",
        parameters={"type": "object", "properties": {}},
        handler=lambda **k: name,
        core=core,
    )


def _tiered_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="battery_status",
            description="Check the Mac's battery level.",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "100%",
            core=True,
        )
    )
    reg.register(
        ToolSpec(
            name="slack__send",
            description="Send a Slack message.",
            parameters={"type": "object", "properties": {}},
            handler=lambda **k: "sent",
        )
    )
    return reg


def test_run_turn_advertises_tiered_tools_when_search_supported(tmp_path: Path) -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="claude-opus-4-8"),
        _tiered_registry(),
        client=FakeClient([resp]),
    )
    _harness(model, tmp_path).run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    sent = model._client.messages.calls[0]["tools"]
    by_name = {t.get("name"): t for t in sent}
    assert "defer_loading" not in by_name["battery_status"]  # core advertised normally
    assert by_name["slack__send"]["defer_loading"] is True  # gated -> deferred
    assert TOOL_SEARCH_NAME in by_name  # search tool present
    assert "defer_loading" not in by_name[TOOL_SEARCH_NAME]  # and never deferred
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}  # cache on the last tool


def test_run_turn_advertises_all_tools_when_search_off(tmp_path: Path) -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_tool_search="off"),  # search disabled
        _tiered_registry(),
        client=FakeClient([resp]),
    )
    _harness(model, tmp_path).run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    sent = model._client.messages.calls[0]["tools"]
    names = {t.get("name") for t in sent}
    assert names == {"battery_status", "slack__send"}  # every tool, legacy shape
    assert TOOL_SEARCH_NAME not in names  # no search tool
    assert all("defer_loading" not in t for t in sent)  # no deferral
    assert all("cache_control" not in t for t in sent)  # legacy request unchanged


def test_tool_search_supported_resolves_mode_and_model() -> None:
    # "off" always disables; "on" always enables; "auto" follows the model table.
    assert tool_search_supported("claude-opus-4-8", "off") is False
    assert tool_search_supported("some-unknown-model", "on") is True
    assert tool_search_supported("claude-opus-4-8", "auto") is True
    assert tool_search_supported("claude-haiku-4-5", "auto") is True  # Haiku 4.5 is in the table
    assert tool_search_supported("some-unknown-model", "auto") is False  # auto still gates by table


def test_partition_tools_splits_core_from_gated() -> None:
    core, gated = partition_tools([_spec("battery", core=True), _spec("slack__send")])
    assert [s.name for s in core] == ["battery"]
    assert [s.name for s in gated] == ["slack__send"]


def test_assemble_marks_gated_defer_and_keeps_core_undeferred() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=True
    )
    by_name = {t["name"]: t for t in tools}
    assert "defer_loading" not in by_name["battery"]  # core advertised normally
    assert by_name["slack__send"]["defer_loading"] is True  # gated -> deferred


def test_assemble_adds_search_tool_not_deferred() -> None:
    tools = assemble_anthropic_tools([_spec("slack__send")], tool_search=True)
    search = next(t for t in tools if t.get("type") == TOOL_SEARCH_TYPE)
    assert search["name"] == TOOL_SEARCH_NAME
    assert "defer_loading" not in search  # the search tool must never be deferred
    # At least one non-deferred tool always exists (the search tool), as required.
    assert any("defer_loading" not in t for t in tools)


def test_assemble_puts_cache_control_on_last_tool_only() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=True
    )
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in tools[:-1])


def test_tool_search_capability_on_for_supported_model() -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="claude-opus-4-8"),
        _registry(),
        client=FakeClient([]),
    )
    assert model._tool_search is True


def test_tool_search_capability_on_for_default_model_in_auto() -> None:
    # Default model (claude-haiku-4-5) is now in the support table -> auto enables search.
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    assert model._tool_search is True


def test_tool_search_capability_off_by_setting_overrides_supported_model() -> None:
    # "off" disables search even for a supported model (the legacy / cost-only escape).
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_tool_search="off"),
        _registry(),
        client=FakeClient([]),
    )
    assert model._tool_search is False


def test_tool_search_capability_forced_on_by_setting() -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_tool_search="on"),
        _registry(),
        client=FakeClient([]),
    )
    assert model._tool_search is True


def test_assemble_fallback_advertises_all_without_defer_or_search() -> None:
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send")], tool_search=False
    )
    names = {t["name"] for t in tools}
    assert names == {"battery", "slack__send"}  # every tool, none dropped
    assert all("defer_loading" not in t for t in tools)  # legacy: no deferral
    assert all(t.get("type") != TOOL_SEARCH_TYPE for t in tools)  # no search tool
    assert all("cache_control" not in t for t in tools)  # legacy request unchanged


def test_assemble_surfaces_relevant_gated_undeferred() -> None:
    # Gated tools named in `relevant` are advertised DIRECTLY (so the model can use them);
    # the rest stay deferred behind the search tool. Core is always direct.
    tools = assemble_anthropic_tools(
        [_spec("battery", core=True), _spec("slack__send"), _spec("github__list")],
        tool_search=True,
        relevant=frozenset({"slack__send"}),
    )
    by_name = {t.get("name"): t for t in tools}
    assert "defer_loading" not in by_name["slack__send"]  # relevant -> directly usable
    assert by_name["github__list"]["defer_loading"] is True  # not relevant -> deferred
    assert "defer_loading" not in by_name["battery"]  # core -> always direct
    assert TOOL_SEARCH_NAME in by_name  # search tool kept as recall net


def test_run_turn_surfaces_query_relevant_gated_tool_undeferred(tmp_path: Path) -> None:
    # The fix for "it only opens but never uses MCP": a gated tool whose name/description
    # matches the user's message is surfaced un-deferred, so the model actually picks it
    # rather than a visible core tool. "send a slack message" matches slack__send.
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", anthropic_model="claude-opus-4-8"),
        _tiered_registry(),
        client=FakeClient([resp]),
    )
    _harness(model, tmp_path).run_turn(
        "send a slack message", lambda c: ToolResult(name=c.name, content="")
    )
    by_name = {t.get("name"): t for t in model._client.messages.calls[0]["tools"]}
    assert "defer_loading" not in by_name["slack__send"]  # surfaced by relevance
    assert TOOL_SEARCH_NAME in by_name  # search tool still present as recall net


def _mcp_spec(name: str, desc: str, *, risk: Risk = Risk.READ_ONLY) -> ToolSpec:
    """A network (MCP) tool spec for _relevant_gated tests."""
    return ToolSpec(
        name=name, description=desc, parameters={}, handler=lambda: name, risk=risk, network=True
    )


def _relevant_gated_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_mcp_spec("github__get_me", "Get details of the authenticated user."))
    reg.register(_mcp_spec("github__search_repositories", "Search for GitHub repositories."))
    reg.register(_mcp_spec("github__list_repository_collaborators", "List repo collaborators."))
    reg.register(_mcp_spec("github__create_issue", "Create an issue.", risk=Risk.WRITE))
    reg.register(_mcp_spec("github__list_notifications", "List your notifications."))
    return reg


def test_relevant_gated_surfaces_identity_and_domain_tool() -> None:
    # Issue #37: "check my public repo stars" must un-defer BOTH the identity anchor
    # (get_me, always) and the stemmed domain match (search_repositories via "repo").
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _relevant_gated_registry(), client=FakeClient([])
    )
    rel = model._relevant_gated("check my public repo stars")
    assert "github__get_me" in rel  # identity anchor — always surfaced
    assert "github__search_repositories" in rel  # stemmed lexical: repo -> repositories


def test_relevant_gated_caps_query_matches_but_keeps_identity() -> None:
    # With the relevant cap at 1, only the single best query match is surfaced, yet the
    # identity anchor is added on top (identity is not subject to the query-match cap).
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic", tool_relevant_limit=1),
        _relevant_gated_registry(),
        client=FakeClient([]),
    )
    rel = model._relevant_gated("search repositories")
    assert "github__search_repositories" in rel  # top query match
    assert "github__get_me" in rel  # identity still added despite the cap
    # The cap bounds the query-matched, non-identity tools to tool_relevant_limit (=1).
    non_identity = rel - {"github__get_me"}
    assert len(non_identity) <= 1


def test_failed_cloud_send_does_not_clear_context_meter(tmp_path: Path) -> None:
    # A failed cloud send rolls back history and returns an error reply, but
    # finalize_turn() should NOT run so the context meter keeps the last successful
    # turn's value (matches the pre-harness run_turn early-return behavior).
    class _FailOnSecond:
        """First call succeeds; second raises an exception."""

        def __init__(self) -> None:
            self.calls = 0

        def create(self, **_kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    content=[_block(type="text", text="First reply")],
                    usage=SimpleNamespace(input_tokens=5, output_tokens=2),
                )
            raise Exception("boom")

    client = SimpleNamespace(messages=_FailOnSecond())
    model = AnthropicLanguageModel(Settings(llm_provider="anthropic"), _registry(), client=client)

    def executor(call: ToolCall) -> ToolResult:
        return ToolResult(name=call.name, content="ok", ok=True)

    # First turn: successful send
    harness = _harness(model, tmp_path)
    reply1 = harness.run_turn("hi", executor)
    assert reply1 == "First reply"
    usage_after_first = harness.context_usage()
    assert usage_after_first is not None
    assert usage_after_first["used"] > 0  # context meter has a value

    # Second turn: failed send
    reply2 = harness.run_turn("again", executor)
    assert "isn't responding" in reply2  # error reply from cloud_error_reply
    usage_after_failed = harness.context_usage()

    # The meter should still show the first turn's value, not be cleared
    assert usage_after_failed == usage_after_first


def test_send_streams_tokens_when_on_event_given() -> None:
    # With on_event given, send() must stream via messages.stream(), emitting a "token"
    # event per text delta, and still return the same ChatResponse the blocking
    # messages.create() path would (text joined, no tool calls).
    model = _make_model_with_streaming_fake(text_deltas=["Hel", "lo"], final_text="Hello")
    events: list[dict[str, Any]] = []
    session = _fresh_session()
    model.begin_turn(session, "hi")
    resp = model.send(session, on_event=events.append)
    assert [e["text"] for e in events if e.get("type") == "token"] == ["Hel", "lo"]
    assert resp.text == "Hello"
    assert resp.tool_calls == []
    # The streaming entry point was used, not the blocking one.
    messages = model._client.messages
    assert isinstance(messages, FakeStreamingMessages)
    assert len(messages.stream_calls) == 1
    assert messages.calls == []


def test_send_without_on_event_uses_blocking_create_not_stream() -> None:
    # on_event=None (the default) must keep using the blocking messages.create() path —
    # messages.stream() is never touched — so behavior for existing callers is unchanged.
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi there.")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    session = _fresh_session()
    model.begin_turn(session, "hi")
    result = model.send(session)
    assert result.text == "Hi there."
    assert len(model._client.messages.calls) == 1


def test_send_omits_temperature() -> None:
    # Sonnet 5 / Opus 4.7+ / Fable 5 reject `temperature` with a 400, so the adapter must
    # not send it (steer via prompt/effort instead).
    resp = SimpleNamespace(
        content=[_block(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    session = _fresh_session()
    model.begin_turn(session, "hi")
    model.send(session)
    assert "temperature" not in model._client.messages.calls[0]


def test_empty_text_block_dropped_from_recorded_assistant_turn() -> None:
    # Sonnet 5 / Opus 4.7+ can emit an empty leading text block alongside tool_use;
    # replaying it 400s ("text content blocks must be non-empty"), so it must be filtered.
    resp = SimpleNamespace(
        content=[
            _block(type="text", text=""),
            _block(type="tool_use", id="t1", name="read_file", input={"path": "x"}),
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    session = _fresh_session()
    model.begin_turn(session, "hi")
    model.send(session)
    blocks = session.history[-1]["content"]
    assert session.history[-1]["role"] == "assistant"
    assert all(not (b["type"] == "text" and b["text"] == "") for b in blocks)
    assert any(b["type"] == "tool_use" for b in blocks)  # tool_use preserved


def test_complete_omits_temperature() -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=1),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    assert model.complete("hi") == "done"
    assert "temperature" not in model._client.messages.calls[0]
