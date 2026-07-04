from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.agent.session import Session
from autobot.config import Settings
from autobot.tools.registry import ToolRegistry


def _session() -> Session:
    return Session(id="t", cwd=".", model="m")


class _Blk:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.usage = _Blk(
            input_tokens=5,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _FakeMessages:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.calls = 0

    def create(self, **kw: Any) -> _Resp:
        self.calls += 1
        return self._resp


class _FakeAnthropic:
    def __init__(self, resp: _Resp) -> None:
        self.messages = _FakeMessages(resp)

    class models:  # noqa: N801 - mimic SDK attribute
        @staticmethod
        def retrieve(model: str) -> Any:
            return _Blk(max_input_tokens=200_000)


def _model(content: list[Any]) -> Any:
    from autobot.llm.anthropic_llm import AnthropicLanguageModel

    client = _FakeAnthropic(_Resp(content))
    return AnthropicLanguageModel(Settings(), ToolRegistry(), client=client)


def test_anthropic_is_a_chat_model() -> None:
    assert isinstance(_model([_Blk(type="text", text="hi")]), ChatModel)


def test_send_returns_text_when_no_tool_use() -> None:
    m = _model([_Blk(type="text", text="hello")])
    s = _session()
    m.begin_turn(s, "hi")
    resp = m.send(s)
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello"
    assert resp.tool_calls == []


def test_send_surfaces_tool_use() -> None:
    m = _model([_Blk(type="tool_use", id="t1", name="get_time", input={})])
    s = _session()
    m.begin_turn(s, "time?")
    resp = m.send(s)
    assert [c.name for c in resp.tool_calls] == ["get_time"]
