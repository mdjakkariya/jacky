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
    apply_text_edits,
    frame_message,
    read_message,
    workspace_edit_files,
)


def _edit(sl: int, sc: int, el: int, ec: int, new: str) -> dict[str, Any]:
    return {
        "range": {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}},
        "newText": new,
    }


def test_apply_text_edits_single() -> None:
    assert apply_text_edits("hello world\n", [_edit(0, 6, 0, 11, "there")]) == "hello there\n"


def test_apply_text_edits_multiple_on_one_line_end_first() -> None:
    # Rename both `foo` -> `bar`; applied end-first so the first edit's offsets stay valid.
    text = "foo = foo + 1\n"
    out = apply_text_edits(text, [_edit(0, 0, 0, 3, "bar"), _edit(0, 6, 0, 9, "bar")])
    assert out == "bar = bar + 1\n"


def test_apply_text_edits_across_lines() -> None:
    text = "a\nold\nc\n"
    assert apply_text_edits(text, [_edit(1, 0, 1, 3, "new")]) == "a\nnew\nc\n"


def test_workspace_edit_files_changes_shape() -> None:
    we = {
        "changes": {
            "file:///a.py": [_edit(0, 0, 0, 3, "x")],
            "file:///b.py": [_edit(1, 0, 1, 1, "y")],
        }
    }
    files = workspace_edit_files(we)
    assert set(files) == {"file:///a.py", "file:///b.py"}
    assert len(files["file:///a.py"]) == 1


def test_workspace_edit_files_document_changes_shape() -> None:
    we = {
        "documentChanges": [
            {"textDocument": {"uri": "file:///a.py"}, "edits": [_edit(0, 0, 0, 1, "z")]}
        ]
    }
    assert list(workspace_edit_files(we)) == ["file:///a.py"]


def test_workspace_edit_files_ignores_malformed() -> None:
    assert workspace_edit_files({"changes": {"file:///a": "not-a-list"}}) == {}
    assert workspace_edit_files({}) == {}


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


class _QueueTransport:
    """A transport that yields pre-scripted messages from ``receive`` (``send`` is a sink)."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._msgs = deque(messages)
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        return self._msgs.popleft() if self._msgs else None


def _diag(uri: str, diags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "method": "textDocument/publishDiagnostics",
        "params": {"uri": uri, "diagnostics": diags},
    }


def test_await_diagnostics_returns_matching_uri() -> None:
    t = _QueueTransport(
        [
            {"method": "window/logMessage", "params": {}},  # unrelated — skipped
            _diag("file:///a.py", [{"message": "boom", "severity": 1}]),
        ]
    )
    assert LspClient(t).await_diagnostics("file:///a.py") == [{"message": "boom", "severity": 1}]


def test_await_diagnostics_none_when_uri_never_reported() -> None:
    t = _QueueTransport([_diag("file:///other.py", [{"message": "x"}])])  # never a.py, then None
    # Timed out without ever hearing about a.py -> None (unknown), NOT [] (falsely "clean").
    assert LspClient(t, timeout=0.05).await_diagnostics("file:///a.py") is None


def test_await_diagnostics_none_on_timeout() -> None:
    assert LspClient(_QueueTransport([]), timeout=0.05).await_diagnostics("file:///a.py") is None


def test_await_diagnostics_returns_empty_list_when_server_reports_clean() -> None:
    # A server that publishes an empty diagnostics list = genuinely clean (distinct from None).
    t = _QueueTransport([_diag("file:///a.py", [])])
    assert LspClient(t).await_diagnostics("file:///a.py") == []


def test_empty_result_is_not_cached() -> None:
    # An empty definition (server maybe still indexing) must NOT be cached — a retry re-asks.
    def responder(msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("method") == "textDocument/definition":
            return [{"jsonrpc": "2.0", "id": msg["id"], "result": None}]
        return []

    transport = _FakeTransport(responder)
    client = LspClient(transport)
    client.definition("file:///a.py", 0, 0)
    client.definition("file:///a.py", 0, 0)
    defs = [m for m in transport.sent if m.get("method") == "textDocument/definition"]
    assert len(defs) == 2  # empty wasn't cached, so the second call re-requested


def test_definition_result_is_cached_per_version() -> None:
    # A repeat definition at the same position on an unchanged file must not hit the server again.
    def responder(msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("method") == "textDocument/definition":
            return [
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"uri": "file:///a.py", "range": {}}}
            ]
        return []

    transport = _FakeTransport(responder)
    client = LspClient(transport)
    client.sync("file:///a.py", "python", "x = 1\n")
    first = client.definition("file:///a.py", 0, 0)
    second = client.definition("file:///a.py", 0, 0)
    assert first == second == [{"uri": "file:///a.py", "range": {}}]
    defs = [m for m in transport.sent if m.get("method") == "textDocument/definition"]
    assert len(defs) == 1  # second call served from cache — one request total


def test_cache_misses_after_the_file_changes() -> None:
    def responder(msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("method") == "textDocument/definition":
            return [{"jsonrpc": "2.0", "id": msg["id"], "result": None}]
        return []

    transport = _FakeTransport(responder)
    client = LspClient(transport)
    client.sync("file:///a.py", "python", "x = 1\n")
    client.definition("file:///a.py", 0, 0)
    client.sync("file:///a.py", "python", "x = 2\n")  # content changed -> version bumps
    client.definition("file:///a.py", 0, 0)
    defs = [m for m in transport.sent if m.get("method") == "textDocument/definition"]
    assert len(defs) == 2  # a new version key -> the cache missed, so a fresh request went out


def test_sync_dedups_open_and_change() -> None:
    transport = _FakeTransport(lambda m: [])  # sync only sends notifications, no responses
    client = LspClient(transport)
    client.sync("file:///a.py", "python", "x = 1\n")  # first sight -> didOpen
    client.sync("file:///a.py", "python", "x = 1\n")  # unchanged -> nothing
    client.sync("file:///a.py", "python", "x = 2\n")  # changed -> didChange
    methods = [m.get("method") for m in transport.sent]
    assert methods == ["textDocument/didOpen", "textDocument/didChange"]
    assert transport.sent[1]["params"]["textDocument"]["version"] == 2
