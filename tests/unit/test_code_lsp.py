"""Tests for the minimal LSP client core (framing + request/response), no real server."""

from __future__ import annotations

import io
from collections import deque
from collections.abc import Callable
from typing import Any

import pytest

from autobot.tools.code.lsp import (
    LspClient,
    LspError,
    frame_message,
    read_message,
)


def test_frame_and_read_round_trip() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": 1}}
    stream = io.BytesIO(frame_message(msg))
    assert read_message(stream) == msg


def test_read_message_eof_returns_none() -> None:
    assert read_message(io.BytesIO(b"")) is None


def test_read_message_malformed_length_returns_none() -> None:
    assert read_message(io.BytesIO(b"Content-Length: notanumber\r\n\r\n{}")) is None


def test_read_two_messages_in_sequence() -> None:
    a = {"id": 1, "result": "a"}
    b = {"id": 2, "result": "b"}
    stream = io.BytesIO(frame_message(a) + frame_message(b))
    assert read_message(stream) == a
    assert read_message(stream) == b


class _FakeTransport:
    """A dict-in/dict-out transport: ``responder`` maps each sent message to replies to enqueue."""

    def __init__(self, responder: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> None:
        self._responder = responder
        self._inbox: deque[dict[str, Any]] = deque()
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        self._inbox.extend(self._responder(message))

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        return self._inbox.popleft() if self._inbox else None


def test_request_correlates_by_id_and_skips_notifications() -> None:
    def responder(msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("method") == "textDocument/definition":
            return [
                # An interleaved notification must be skipped, not mistaken for the response.
                {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"m": "hi"}},
                {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"n": 1}},
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"uri": "file:///a.py", "range": {}}},
            ]
        return []

    client = LspClient(_FakeTransport(responder))
    locs = client.definition("file:///a.py", 3, 5)
    assert locs == [{"uri": "file:///a.py", "range": {}}]
    assert len(client.diagnostics) == 1  # the diagnostics notification was collected


def test_request_raises_on_error_response() -> None:
    client = LspClient(
        _FakeTransport(lambda m: [{"jsonrpc": "2.0", "id": m["id"], "error": {"message": "boom"}}])
    )
    with pytest.raises(LspError):
        client.request("textDocument/definition", {})


def test_request_raises_when_server_closes() -> None:
    client = LspClient(_FakeTransport(lambda m: []))  # no reply → receive() returns None
    with pytest.raises(LspError):
        client.request("initialize", {})


def test_references_normalises_location_link() -> None:
    def responder(msg: dict[str, Any]) -> list[dict[str, Any]]:
        # A server that returns LocationLink[] (targetUri/targetSelectionRange).
        return [
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": [{"targetUri": "file:///b.py", "targetSelectionRange": {"line": 2}}],
            }
        ]

    client = LspClient(_FakeTransport(responder))
    locs = client.references("file:///a.py", 1, 1)
    assert locs == [{"uri": "file:///b.py", "range": {"line": 2}}]


def _echo_ok(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Responder that answers every request with an empty result (notifications get nothing)."""
    return [{"jsonrpc": "2.0", "id": m["id"], "result": {}}] if "id" in m else []


def test_initialize_declares_capabilities_then_initialized() -> None:
    transport = _FakeTransport(_echo_ok)
    LspClient(transport).initialize("file:///repo")
    assert [m.get("method") for m in transport.sent] == ["initialize", "initialized"]
    caps = transport.sent[0]["params"]["capabilities"]["textDocument"]
    assert "definition" in caps and "references" in caps and "rename" in caps


def test_request_times_out_when_no_response() -> None:
    # A transport that never yields a message must not hang — the client gives up (falls back).
    client = LspClient(_FakeTransport(lambda m: []), timeout=0.05)
    with pytest.raises(LspError):
        client.request("textDocument/definition", {})


def test_sync_dedups_open_and_change() -> None:
    transport = _FakeTransport(lambda m: [])  # sync only sends notifications, no responses
    client = LspClient(transport)
    client.sync("file:///a.py", "python", "x = 1\n")  # first sight -> didOpen
    client.sync("file:///a.py", "python", "x = 1\n")  # unchanged -> nothing
    client.sync("file:///a.py", "python", "x = 2\n")  # changed -> didChange
    methods = [m.get("method") for m in transport.sent]
    assert methods == ["textDocument/didOpen", "textDocument/didChange"]
    assert transport.sent[1]["params"]["textDocument"]["version"] == 2
