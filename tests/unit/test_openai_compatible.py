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


class _Delta:
    """Fake ``ChoiceDelta``: a streamed chunk's content/tool_call fragments."""

    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _ToolCallFrag:
    """Fake ``ChoiceDeltaToolCall``: one fragment of a streamed tool call."""

    def __init__(
        self,
        index: int,
        call_id: str | None = None,
        name: str | None = None,
        args: str | None = None,
    ) -> None:
        self.index = index
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": args})()


class _StreamChunk:
    def __init__(self, delta: _Delta | None, usage: _Usage | None = None) -> None:
        self.choices = [type("Ch", (), {"delta": delta})()] if delta is not None else []
        self.usage = usage


class _FakeStreamingCompletions:
    def __init__(self, chunks: list[_StreamChunk], resp: _Resp) -> None:
        self._chunks = chunks
        self._resp = resp
        self.sent: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> Any:
        self.sent.append(kw)
        if kw.get("stream"):
            return iter(self._chunks)
        return self._resp


class _FakeStreamingOpenAI:
    def __init__(self, chunks: list[_StreamChunk], resp: _Resp) -> None:
        self.chat = type("C", (), {"completions": _FakeStreamingCompletions(chunks, resp)})()


def _make_openai_model_with_stream(
    content_deltas: list[str], tool_frag: list[dict[str, Any]]
) -> OpenAICompatibleModel:
    """Build a model whose fake client streams ``content_deltas`` + fragmented tool calls."""
    chunks: list[_StreamChunk] = [_StreamChunk(_Delta(content=piece)) for piece in content_deltas]
    for frag in tool_frag:
        tc = _ToolCallFrag(
            index=frag["index"],
            call_id=frag.get("id"),
            name=frag.get("name"),
            args=frag.get("args"),
        )
        chunks.append(_StreamChunk(_Delta(tool_calls=[tc])))
    chunks.append(_StreamChunk(None, usage=_Usage(10, 4)))  # final usage-only chunk
    client = _FakeStreamingOpenAI(chunks, _Resp(_Msg("unused")))
    return OpenAICompatibleModel(
        Settings(llm_provider="openai", openai_base_url="http://x/v1", llm_model="gpt-x"),
        ToolRegistry(),
        client=client,
    )


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


def test_create_streams_tokens_and_assembles_tool_calls() -> None:
    # Fake completions.create(stream=True) yields content + fragmented tool_call deltas.
    model = _make_openai_model_with_stream(
        content_deltas=["Wo", "rld"],
        tool_frag=[  # one tool call assembled from fragments (index 0)
            {"index": 0, "id": "call_1", "name": "read_file", "args": '{"pa'},
            {"index": 0, "args": 'th": "a.py"}'},
        ],
    )
    events: list[dict[str, Any]] = []
    s = _session()
    model.begin_turn(s, "hi")
    resp = model.send(s, on_event=events.append)
    assert [e["text"] for e in events if e.get("type") == "token"] == ["Wo", "rld"]
    assert resp.text == "World"
    assert resp.tool_calls and resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "a.py"}


def test_send_without_on_event_uses_blocking_path_unchanged() -> None:
    # No on_event => the non-streaming client path (kwargs never carry stream=True).
    m = _model(_Resp(_Msg("hello there", tool_calls=None)))
    s = _session()
    m.begin_turn(s, "hi")
    resp = m.send(s, on_event=None)
    assert resp.text == "hello there"


def test_streaming_does_not_emit_events_for_tool_call_fragments() -> None:
    # Only text deltas become "token" events; tool-call fragments are assembled silently.
    model = _make_openai_model_with_stream(
        content_deltas=[],
        tool_frag=[{"index": 0, "id": "call_2", "name": "get_time", "args": "{}"}],
    )
    events: list[dict[str, Any]] = []
    s = _session()
    model.begin_turn(s, "time?")
    resp = model.send(s, on_event=events.append)
    assert events == []
    assert resp.tool_calls and resp.tool_calls[0].name == "get_time"
