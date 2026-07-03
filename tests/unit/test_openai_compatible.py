from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.agent.providers.openai_compatible import OpenAICompatibleModel
from autobot.agent.session import Session
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry


def _session() -> Session:
    return Session(id="t", cwd=".", model="m")


class _Msg:
    def __init__(self, content: str | None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _ToolCallObj:
    def __init__(self, call_id: str | None, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Choice:
    def __init__(self, message: _Msg) -> None:
        self.message = message


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Resp:
    def __init__(self, message: _Msg) -> None:
        self.choices = [_Choice(message)]
        self.usage = _Usage(10, 4)


class _FakeCompletions:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.sent: list[list[dict[str, Any]]] = []

    def create(self, *, messages: list[dict[str, Any]], **kw: Any) -> _Resp:
        self.sent.append(messages)
        return self._resp


class _FakeOpenAI:
    def __init__(self, resp: _Resp) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(resp)})()


def _model(resp: _Resp) -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=_FakeOpenAI(resp),
    )


def test_is_a_chat_model() -> None:
    assert isinstance(_model(_Resp(_Msg("hi"))), ChatModel)


def test_send_returns_text_when_no_tool_calls() -> None:
    m = _model(_Resp(_Msg("hello there", tool_calls=None)))
    s = _session()
    m.begin_turn(s, "hi")
    resp = m.send(s)
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello there"
    assert resp.tool_calls == []


def test_send_parses_tool_calls_with_json_arguments() -> None:
    tc = _ToolCallObj("call_1", "get_time", '{"tz": "utc"}')
    m = _model(_Resp(_Msg(None, tool_calls=[tc])))
    s = _session()
    m.begin_turn(s, "time?")
    resp = m.send(s)
    assert [c.name for c in resp.tool_calls] == ["get_time"]
    assert resp.tool_calls[0].arguments == {"tz": "utc"}


def test_record_results_appends_tool_message_with_tool_call_id() -> None:
    tc = _ToolCallObj("call_9", "get_time", "{}")
    client = _FakeOpenAI(_Resp(_Msg(None, tool_calls=[tc])))
    m = OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=client,
    )
    s = _session()
    m.begin_turn(s, "go")
    m.send(s)  # sets the last assistant tool_calls (id call_9)
    m.record_results(s, [(ToolCall(name="get_time"), ToolResult(name="get_time", content="noon"))])
    m.send(s)
    last = client.chat.completions.sent[-1]
    tool_msgs = [x for x in last if x.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_9"
    assert tool_msgs[0]["content"] == "noon"


def test_idless_duplicate_tool_calls_get_distinct_tool_call_ids() -> None:
    # Two id-less calls to the same tool in one round: the fallback must not collapse
    # both to the same id, or OpenAI's uniqueness requirement (and result pairing) breaks.
    tc1 = _ToolCallObj(None, "get_time", '{"tz": "utc"}')
    tc2 = _ToolCallObj(None, "get_time", '{"tz": "pst"}')
    client = _FakeOpenAI(_Resp(_Msg(None, tool_calls=[tc1, tc2])))
    m = OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=client,
    )
    s = _session()
    m.begin_turn(s, "times?")
    resp = m.send(s)
    m.record_results(
        s,
        [
            (resp.tool_calls[0], ToolResult(name="get_time", content="noon")),
            (resp.tool_calls[1], ToolResult(name="get_time", content="4am")),
        ],
    )
    m.send(s)
    last = client.chat.completions.sent[-1]
    tool_msgs = [x for x in last if x.get("role") == "tool"]
    ids = [msg["tool_call_id"] for msg in tool_msgs]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"expected distinct tool_call_ids, got {ids}"


def test_bad_json_arguments_degrade_to_empty_dict() -> None:
    tc = _ToolCallObj("c", "t", "not json")
    m = _model(_Resp(_Msg(None, tool_calls=[tc])))
    s = _session()
    m.begin_turn(s, "x")
    resp = m.send(s)
    assert resp.tool_calls[0].arguments == {}
