"""Tests for the Ollama backend's multi-round tool loop, with a fake client.

No Ollama server: a fake client returns canned chat responses, so the loop is
exercised entirely offline. Mirrors the pattern in test_anthropic_llm.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from autobot.agent.harness import AgentHarness
from autobot.agent.session_store import SessionStore
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.tools.registry import ToolRegistry, ToolSpec
from autobot.tools.selection import LexicalToolSelector


def _harness(model: OllamaLanguageModel, tmp_path: Path) -> AgentHarness:
    return AgentHarness(model, SessionStore(str(tmp_path)))


def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": args}}


def _resp(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    msg = {"role": "assistant", "content": content, "tool_calls": tool_calls or []}
    return SimpleNamespace(message=msg, prompt_eval_count=10, eval_count=5)


class _FakeOllama:
    """Returns queued chat responses; records the messages it was called with."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def show(self, _model: str) -> dict[str, Any]:  # _resolve_context fallback
        return {}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="list_files",
            description="List files",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=lambda path="": f"listed {path}",
        )
    )
    reg.register(
        ToolSpec(
            name="open_path",
            description="Open a path",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=lambda path="": f"opened {path}",
        )
    )
    return reg


def _model(responses: list[Any]) -> OllamaLanguageModel:
    # context_tokens override skips the client.show() lookup path entirely.
    return OllamaLanguageModel(
        Settings(context_tokens=4096), _registry(), client=_FakeOllama(responses)
    )


def test_run_turn_no_tools_returns_text(tmp_path: Path) -> None:
    model = _model([_resp(content="Hello there.")])
    assert (
        _harness(model, tmp_path).run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
        == "Hello there."
    )


def test_run_turn_chains_two_tools_in_one_turn(tmp_path: Path) -> None:
    # Round 1: list_files. Round 2 (using the result): open_path. Round 3: final text.
    responses = [
        _resp(tool_calls=[_tc("list_files", {"path": "~/Downloads"})]),
        _resp(tool_calls=[_tc("open_path", {"path": "~/Downloads/latest.png"})]),
        _resp(content="Opened your latest screenshot."),
    ]
    model = _model(responses)
    executed: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call.name)
        return ToolResult(name=call.name, content="ok", ok=True)

    reply = _harness(model, tmp_path).run_turn("open my latest screenshot", execute)
    assert reply == "Opened your latest screenshot."
    assert executed == ["list_files", "open_path"]  # chained across rounds


def test_run_turn_does_not_rerun_a_failing_tool_call(tmp_path: Path) -> None:
    # The model re-issues the same failing call; the loop runs it once, then stops.
    responses = [
        _resp(tool_calls=[_tc("open_path", {"path": "/nope"})]),
        _resp(tool_calls=[_tc("open_path", {"path": "/nope"})]),
    ]
    model = _model(responses)
    runs = {"n": 0}

    def execute(call: ToolCall) -> ToolResult:
        runs["n"] += 1
        return ToolResult(name=call.name, content="No access. Do NOT retry.", ok=False)

    reply = _harness(model, tmp_path).run_turn("open it", execute)
    assert runs["n"] == 1  # the identical repeat was short-circuited
    assert "do not retry" in reply.lower()


def test_run_turn_forces_final_answer_at_round_cap(tmp_path: Path) -> None:
    # 8 rounds all ask for a (distinct) tool, never converging; at the cap a final
    # tools-disabled call synthesizes the reply (not a canned apology).
    responses = [_resp(tool_calls=[_tc("list_files", {"path": f"/p{i}"})]) for i in range(8)]
    responses.append(_resp(content="Here's what I found so far."))  # forced final, no tools
    model = _model(responses)
    reply = _harness(model, tmp_path).run_turn(
        "dig forever", lambda c: ToolResult(name=c.name, content="ok", ok=True)
    )
    assert reply == "Here's what I found so far."
    # The final call was made with tools disabled.
    assert "tools" not in model._client.calls[-1]


def test_run_turn_mixed_round_continues_when_one_call_is_new(tmp_path: Path) -> None:
    # A round that mixes a previously-failed repeat with a brand-new call must NOT
    # stop early: the new call runs and the loop proceeds to a final answer.
    responses = [
        _resp(tool_calls=[_tc("open_path", {"path": "/nope"})]),  # round 1: fails
        _resp(  # round 2: the failed repeat + a new call
            tool_calls=[
                _tc("open_path", {"path": "/nope"}),
                _tc("list_files", {"path": "~/Downloads"}),
            ]
        ),
        _resp(content="Here's the listing."),  # round 3: final text
    ]
    model = _model(responses)
    runs: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        runs.append(call.name)
        ok = call.name != "open_path"
        return ToolResult(name=call.name, content="ok" if ok else "No access.", ok=ok)

    reply = _harness(model, tmp_path).run_turn("open then list", execute)
    assert reply == "Here's the listing."  # did not stop early
    assert runs == ["open_path", "list_files"]  # failed repeat not re-run; new call ran once


def test_history_keeps_tool_messages_across_turns(tmp_path: Path) -> None:
    # Turn 1 runs a tool; turn 2 must see the prior tool exchange in the sent messages.
    model = _model(
        [
            _resp(tool_calls=[_tc("open_path", {"path": "~/a"})]),
            _resp(content="Opened it."),
            _resp(content="Closed it."),
        ]
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("open a", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    harness.run_turn("close it", lambda c: ToolResult(name=c.name, content="ok", ok=True))
    sent = model._client.calls[-1]["messages"]
    roles = [m.get("role") for m in sent]
    assert "tool" in roles  # the prior turn's tool result is carried into turn 2


def test_selector_gates_advertised_tools(tmp_path: Path) -> None:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="battery_status",
            description="Check the Mac's battery level and charging state.",
            parameters={},
            handler=lambda: "100%",
            core=True,
        )
    )
    reg.register(
        ToolSpec(
            name="slack__send",
            description="Send a message to a Slack channel.",
            parameters={},
            handler=lambda **k: "sent",
        )
    )
    selector = LexicalToolSelector(reg, budget=20, core_extra=frozenset(), core_remove=frozenset())
    client = _FakeOllama([_resp(content="100%.")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=selector
    )
    _harness(model, tmp_path).run_turn(
        "what's my battery?", lambda c: ToolResult(name=c.name, content="")
    )
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert "battery_status" in advertised  # core, always advertised
    assert "slack__send" not in advertised  # gated, irrelevant to a battery query


def test_no_selector_advertises_all_tools(tmp_path: Path) -> None:
    client = _FakeOllama([_resp(content="hi")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), _registry(), client=client
    )  # no selector → legacy behavior
    _harness(model, tmp_path).run_turn("hi", lambda c: ToolResult(name=c.name, content=""))
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert advertised == {"list_files", "open_path"}


def _battery_slack_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="battery_status",
            description="Check the Mac's battery level and charging state.",
            parameters={},
            handler=lambda: "100%",
            core=True,
        )
    )
    reg.register(
        ToolSpec(
            name="slack__send",
            description="Send a message to a Slack channel.",
            parameters={},
            handler=lambda **k: "sent",
        )
    )
    return reg


def _selector(reg: ToolRegistry) -> LexicalToolSelector:
    return LexicalToolSelector(reg, budget=20, core_extra=frozenset(), core_remove=frozenset())


def test_find_tools_is_always_advertised_with_selector(tmp_path: Path) -> None:
    reg = _battery_slack_registry()
    client = _FakeOllama([_resp(content="100%.")])
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    _harness(model, tmp_path).run_turn(
        "what's my battery?", lambda c: ToolResult(name=c.name, content="")
    )
    advertised = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert "find_tools" in advertised  # always, even when no gated tool matched
    assert "battery_status" in advertised  # core
    assert "slack__send" not in advertised  # gated + irrelevant → not yet advertised


def test_find_tools_call_pins_matches_for_next_round(tmp_path: Path) -> None:
    reg = _battery_slack_registry()
    # Round 1: the model can't see a slack tool, so it calls find_tools.
    # Round 2: it should now see slack__send (pinned) and call it.
    # Round 3: final text.
    responses = [
        _resp(tool_calls=[_tc("find_tools", {"intent": "send a message on slack"})]),
        _resp(tool_calls=[_tc("slack__send", {"text": "hi"})]),
        _resp(content="Sent your message."),
    ]
    client = _FakeOllama(responses)
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    executed: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        executed.append(call.name)
        return ToolResult(name=call.name, content="sent", ok=True)

    reply = _harness(model, tmp_path).run_turn("tell the team hi on slack", execute)
    assert reply == "Sent your message."
    # find_tools was NOT dispatched through the executor (the loop owns it):
    assert executed == ["slack__send"]
    # Round 2's advertised set includes the pinned slack__send:
    round2_tools = {t["function"]["name"] for t in client.calls[1]["tools"]}
    assert "slack__send" in round2_tools
    # The find_tools result was fed back as a tool message in round 2's prompt:
    round2_msgs = client.calls[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_name") == "find_tools" for m in round2_msgs)


def test_pins_reset_between_turns(tmp_path: Path) -> None:
    reg = _battery_slack_registry()
    responses = [
        _resp(tool_calls=[_tc("find_tools", {"intent": "send a message on slack"})]),
        _resp(content="Found it."),  # turn 1, round 2: final
        _resp(content="100%."),  # turn 2, round 1: final
    ]
    client = _FakeOllama(responses)
    model = OllamaLanguageModel(
        Settings(context_tokens=4096), reg, client=client, selector=_selector(reg)
    )
    harness = _harness(model, tmp_path)
    harness.run_turn("message slack", lambda c: ToolResult(name=c.name, content="", ok=True))
    assert "slack__send" in model._pinned  # pinned during turn 1
    harness.run_turn("what's my battery?", lambda c: ToolResult(name=c.name, content="", ok=True))
    assert model._pinned == set()  # reset at the start of turn 2
    # Turn 2's first round must NOT carry turn 1's pin into the advertised set:
    turn2_tools = {t["function"]["name"] for t in client.calls[-1]["tools"]}
    assert "slack__send" not in turn2_tools
