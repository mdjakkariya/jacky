from __future__ import annotations

from typing import Any

from autobot.agent.chat_model import ChatModel, ChatResponse
from autobot.agent.session import Session
from autobot.config import Settings
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.registry import ToolRegistry


class _FakeOllamaClient:
    """Returns a scripted chat response; records the messages it was sent."""

    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message
        self.sent: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.sent.append(messages)
        return {"message": self._message, "prompt_eval_count": 5, "eval_count": 3}

    def show(self, model: str) -> dict[str, Any]:
        return {"modelinfo": {"qwen2.context_length": 4096}}


def _model(message: dict[str, Any]) -> Any:
    from autobot.llm.ollama_llm import OllamaLanguageModel

    return OllamaLanguageModel(Settings(), ToolRegistry(), client=_FakeOllamaClient(message))


def test_ollama_is_a_chat_model() -> None:
    assert isinstance(_model({"content": "hi"}), ChatModel)


def _session() -> Session:
    return Session(id="t", cwd=".", model="m")


def test_begin_then_send_returns_text_when_no_tool_calls() -> None:
    m = _model({"content": "hello there", "tool_calls": []})
    s = _session()
    m.begin_turn(s, "hi")
    resp = m.send(s)
    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello there"
    assert resp.tool_calls == []


def test_send_surfaces_tool_calls() -> None:
    m = _model({"content": "", "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}]})
    s = _session()
    m.begin_turn(s, "time?")
    resp = m.send(s)
    assert [c.name for c in resp.tool_calls] == ["get_time"]


def test_record_results_appends_tool_messages() -> None:
    client = _FakeOllamaClient({"content": "done", "tool_calls": []})
    from autobot.llm.ollama_llm import OllamaLanguageModel

    m = OllamaLanguageModel(Settings(), ToolRegistry(), client=client)
    s = _session()
    m.begin_turn(s, "go")
    m.send(s)
    m.record_results(s, [(ToolCall(name="get_time"), ToolResult(name="get_time", content="noon"))])
    m.send(s)  # second send must include the tool result in the messages
    roles = [msg.get("role") for msg in client.sent[-1]]
    assert "tool" in roles


class _FakeStreamingOllamaClient:
    """Fake Ollama client whose ``chat(stream=True)`` yields scripted chunks."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks
        self.sent: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        assert kwargs.get("stream") is True  # the streaming branch always sets this
        return iter(self._chunks)

    def show(self, model: str) -> dict[str, Any]:
        return {"modelinfo": {"qwen2.context_length": 4096}}


def _streaming_model(chunks: list[dict[str, Any]]) -> Any:
    from autobot.llm.ollama_llm import OllamaLanguageModel

    return OllamaLanguageModel(
        Settings(), ToolRegistry(), client=_FakeStreamingOllamaClient(chunks)
    )


def test_chat_streams_tokens_and_assembles_message() -> None:
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": "Hel"}},
        {
            "message": {"role": "assistant", "content": "lo"},
            "done": True,
            "prompt_eval_count": 3,
            "eval_count": 2,
        },
    ]
    model = _streaming_model(chunks)
    events: list[dict[str, Any]] = []
    session = _session()
    model.begin_turn(session, "hi")
    resp = model.send(session, on_event=events.append)
    assert [e["text"] for e in events if e.get("type") == "token"] == ["Hel", "lo"]
    assert resp.text == "Hello"
    assert resp.tool_calls == []
    # Usage bookkeeping updates from the final chunk's counts, same as the blocking path.
    assert model._last_prompt_tokens == 3
    assert model._last_eval_tokens == 2


def test_chat_stream_preserves_inter_token_whitespace() -> None:
    # A per-token strip would glue words together across chunk boundaries; the
    # streaming branch must only strip the whole assembled message, like the
    # blocking path does.
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": "Hello"}},
        {"message": {"role": "assistant", "content": " world"}, "done": True},
    ]
    model = _streaming_model(chunks)
    session = _session()
    model.begin_turn(session, "hi")
    resp = model.send(session, on_event=lambda _e: None)
    assert resp.text == "Hello world"


def test_chat_stream_collects_whole_tool_call_from_final_chunk() -> None:
    chunks: list[dict[str, Any]] = [
        {"message": {"role": "assistant", "content": ""}},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_time", "arguments": {}}}],
            },
            "done": True,
        },
    ]
    model = _streaming_model(chunks)
    session = _session()
    model.begin_turn(session, "time?")
    resp = model.send(session, on_event=lambda _e: None)
    assert [c.name for c in resp.tool_calls] == ["get_time"]


def test_chat_stream_passes_think_for_qwen3_model() -> None:
    # The blocking path threads `think=think_on` for qwen3 models (I1); the streaming
    # path must do the same, or a streamed turn silently ignores `llm_think`.
    chunks: list[dict[str, Any]] = [{"message": {"role": "assistant", "content": "hi"}}]
    client = _FakeStreamingOllamaClient(chunks)
    from autobot.llm.ollama_llm import OllamaLanguageModel

    model = OllamaLanguageModel(
        Settings(llm_model="qwen3:8b", llm_think=True), ToolRegistry(), client=client
    )
    session = _session()
    model.begin_turn(session, "hi")
    model.send(session, on_event=lambda _e: None)
    assert client.sent[-1].get("think") is True


def test_chat_stream_falls_back_without_think_on_type_error() -> None:
    # If the installed ollama client's chat() rejects the `think` kwarg (older
    # versions), the streaming branch must retry without it, like the blocking path.
    chunks: list[dict[str, Any]] = [{"message": {"role": "assistant", "content": "hi"}}]

    class _RejectsThinkClient:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        def chat(self, **kwargs: Any) -> Any:
            if "think" in kwargs:
                raise TypeError("chat() got an unexpected keyword argument 'think'")
            self.sent.append(kwargs)
            assert kwargs.get("stream") is True
            return iter(chunks)

        def show(self, model: str) -> dict[str, Any]:
            return {"modelinfo": {"qwen2.context_length": 4096}}

    from autobot.llm.ollama_llm import OllamaLanguageModel

    client = _RejectsThinkClient()
    model = OllamaLanguageModel(
        Settings(llm_model="qwen3:8b", llm_think=True), ToolRegistry(), client=client
    )
    session = _session()
    model.begin_turn(session, "hi")
    resp = model.send(session, on_event=lambda _e: None)
    assert resp.text == "hi"
    assert "think" not in client.sent[-1]


def test_chat_stream_not_used_when_on_event_is_none() -> None:
    # Without on_event, _chat() must take the blocking path: no stream kwarg, and
    # the client's chat() is called once (not iterated as a stream).
    calls: list[dict[str, Any]] = []

    class _BlockingClient:
        def chat(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"message": {"role": "assistant", "content": "hi", "tool_calls": []}}

        def show(self, model: str) -> dict[str, Any]:
            return {"modelinfo": {"qwen2.context_length": 4096}}

    from autobot.llm.ollama_llm import OllamaLanguageModel

    m = OllamaLanguageModel(Settings(), ToolRegistry(), client=_BlockingClient())
    s = _session()
    m.begin_turn(s, "hi")
    resp = m.send(s)  # on_event defaults to None
    assert resp.text == "hi"
    assert "stream" not in calls[-1]
