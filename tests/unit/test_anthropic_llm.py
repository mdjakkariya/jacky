"""Tests for the Anthropic backend — pure helpers + a turn with a fake client.

No network and no API key: a fake client returns canned responses, so the
tool-calling loop is exercised entirely offline.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
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


def test_run_turn_executes_tool_then_returns_final_text() -> None:
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

    reply = model.run_turn("open safari", execute)
    assert reply == "Opened Safari for you."
    assert executed == [ToolCall(name="open_app", arguments={"name": "Safari"})]
    # Second API call carried the tool_result back to the model.
    second = model._client.messages.calls[1]
    assert any(
        isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
        for m in second["messages"]
        if isinstance(m.get("content"), list)
    )


def test_run_turn_stops_repeating_a_failing_tool_call() -> None:
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

    reply = model.run_turn("open it", execute)
    assert runs["n"] == 1  # ran once; the identical repeat was short-circuited
    assert "do not retry" in reply.lower()


def test_history_keeps_tool_blocks_across_turns() -> None:
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
    model.run_turn("open safari", lambda c: ToolResult(name=c.name, content="Opened Safari."))
    model.run_turn("close it", lambda c: ToolResult(name=c.name, content=""))

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


def test_recovers_from_prompt_too_long_by_trimming_and_retrying() -> None:
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    for i in range(6):  # several old turns to trim away
        model._history.append({"role": "user", "content": f"old {i}"})
        model._history.append({"role": "assistant", "content": [{"type": "text", "text": f"r{i}"}]})
    msgs = _TooLongThenOk(fail_times=3)
    model._client = SimpleNamespace(messages=msgs)

    reply = model.run_turn("new question", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok now"  # recovered, not the error reply
    assert msgs.calls == 4  # 3 rejections (each drops a turn) + 1 success
    assert len(model._history) < 14  # oldest turns were trimmed away


def test_proactive_trim_drops_a_giant_old_turn_before_sending() -> None:
    responses = [
        SimpleNamespace(
            content=[_block(type="text", text="hi")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )
    ]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient(responses)
    )
    model._history.append({"role": "user", "content": "x" * 1_000_000})  # ~250k tokens
    model._history.append({"role": "assistant", "content": [{"type": "text", "text": "y"}]})

    model.run_turn("hello", lambda c: ToolResult(name=c.name, content=""))
    joined = "".join(str(m.get("content", "")) for m in model._history)
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


def test_learns_smaller_window_from_error_then_fits() -> None:
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
    for i in range(4):
        model._history.append({"role": "user", "content": f"old {i}"})
        model._history.append({"role": "assistant", "content": [{"type": "text", "text": "r"}]})
    model._client = SimpleNamespace(messages=_Small())
    reply = model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok"
    assert model.context_window == 32768  # learned from the rejection, not hardcoded


def test_compaction_summarizes_older_turns_and_keeps_recent() -> None:
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
    for i in range(30):  # plenty of older turns to compact
        model._history.append({"role": "user", "content": f"u{i}"})
        model._history.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})

    reply = model.run_turn("now", lambda c: ToolResult(name=c.name, content=""))
    assert reply == "ok"
    assert model._summary == "OLDER STUFF SUMMARY"  # older turns folded into a summary
    assert len(model._history) <= 21  # only the recent tail kept verbatim
    assert "OLDER STUFF SUMMARY" in model._system()  # summary injected into the system prompt


def test_new_session_clears_history_summary_and_usage() -> None:
    # "New chat" must wipe the conversation and reset the context meter to empty.
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
    )
    model._history.append({"role": "user", "content": "hi"})
    model._summary = "earlier stuff"
    model._last_prompt_total = 5000
    model._last_cache_read = 4000
    model._last_turn_in = 900

    model.new_session()

    assert model._history == []
    assert model._summary == ""
    assert model._last_prompt_total == 0
    assert model._last_cache_read == 0
    assert model._last_turn_in == 0
    assert model.context_usage() is None  # meter reads empty until the next turn


def test_too_long_even_after_trim_returns_calm_reply_and_rolls_back() -> None:
    class _AlwaysTooLong:
        def create(self, **_kwargs: Any) -> Any:
            raise RuntimeError("prompt is too long: 300000 tokens > 200000 maximum")

    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"),
        _registry(),
        client=SimpleNamespace(messages=_AlwaysTooLong()),
    )
    reply = model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert reply == too_long_reply()
    assert model._history == []  # the half-built turn was rolled back


def test_run_turn_no_tools_returns_text() -> None:
    responses = [SimpleNamespace(content=[_block(type="text", text="Hello there.")])]
    model = AnthropicLanguageModel(Settings(), _registry(), client=FakeClient(responses))
    assert model.run_turn("hi", lambda c: ToolResult(name=c.name, content="")) == "Hello there."


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


def test_run_turn_returns_calm_reply_on_api_error() -> None:
    err = RuntimeError("Error code: 404")
    err.body = {"error": {"message": "model: claude-3-5-haiku-latest"}}  # type: ignore[attr-defined]
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=BoomClient(err)
    )
    reply = model.run_turn("how are you", lambda c: ToolResult(name=c.name, content=""))
    assert "isn't responding" in reply
    assert "404" not in reply


def test_estimate_cost_usd_known_model() -> None:
    # Haiku 4.5 is $1/$5 per MTok: 1M in + 1M out = $1 + $5 = $6.
    assert estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_estimate_cost_usd_unknown_model_returns_none() -> None:
    assert estimate_cost_usd("some-future-model", 100, 100) is None


def test_context_usage_reports_session_price_for_priced_model() -> None:
    # Default model (claude-haiku-4-5) is in the pricing table: 1M in @ $1 + 1M out @ $5 = $6.
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    usage = model.context_usage()
    assert usage is not None
    assert usage["price"] == 6.0


def test_context_usage_price_is_none_for_unpriced_model() -> None:
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
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    usage = model.context_usage()
    assert usage is not None
    assert usage["price"] is None


def test_session_price_resets_on_new_session() -> None:
    # "Price of the current session" must start fresh on New chat, not carry over.
    def _resp() -> SimpleNamespace:
        return SimpleNamespace(
            content=[_block(type="text", text="Hi.")],
            usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([_resp(), _resp()])
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    model.new_session()
    model.run_turn("hi again", lambda c: ToolResult(name=c.name, content=""))
    usage = model.context_usage()
    assert usage is not None
    assert usage["price"] == 6.0  # one turn's cost, not two accumulated


def test_run_turn_accumulates_token_usage() -> None:
    resp = SimpleNamespace(
        content=[_block(type="text", text="Hi.")],
        usage=SimpleNamespace(input_tokens=120, output_tokens=8),
    )
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([resp])
    )
    model.run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    assert model._session_in == 120
    assert model._session_out == 8


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
    sys = model._system()
    assert "MD" in sys and "Jack" in sys


def test_run_turn_forces_final_answer_at_round_cap() -> None:
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
    reply = model.run_turn("loop", lambda c: ToolResult(name=c.name, content="ok", ok=True))
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


def test_tool_search_supported_resolves_mode_and_model() -> None:
    # "off" always disables; "on" always enables; "auto" follows the model table.
    assert tool_search_supported("claude-opus-4-8", "off") is False
    assert tool_search_supported("some-unknown-model", "on") is True
    assert tool_search_supported("claude-opus-4-8", "auto") is True
    assert tool_search_supported("claude-haiku-4-5", "auto") is False  # not in the table


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


def test_tool_search_capability_off_for_default_model_in_auto() -> None:
    # Default model (claude-haiku-4-5) is not in the support table -> auto disables it.
    model = AnthropicLanguageModel(
        Settings(llm_provider="anthropic"), _registry(), client=FakeClient([])
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
