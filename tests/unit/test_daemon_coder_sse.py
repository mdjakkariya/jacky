"""The SSE frame generator serializes coder events to text/event-stream frames."""

from __future__ import annotations

from fastapi.testclient import TestClient

from autobot.core.events import EventBus
from autobot.daemon.server import _sse_frames, create_app


def test_sse_frames_serializes_each_event() -> None:
    events = [
        {"type": "tool", "event": "start", "name": "read_file", "label": "Read a.py"},
        {"status": "done", "reply": "ok"},
    ]
    frames = list(_sse_frames(iter(events)))
    expected_tool = (
        'data: {"type": "tool", "event": "start", "name": "read_file", "label": "Read a.py"}\n\n'
    )
    assert frames[0] == expected_tool
    assert frames[1] == 'data: {"status": "done", "reply": "ok"}\n\n'


def test_sse_frames_survives_a_bad_event() -> None:
    # A non-serializable event degrades to an error frame rather than raising.
    frames = list(_sse_frames(iter([{"x": object()}])))
    assert frames and frames[0].startswith("data: ")


def test_coder_events_route_exists_and_is_empty_when_unwired() -> None:
    # No on_coder_events → the route exists (200, not 404) and the stream is empty. The live
    # settle→event→parse path is covered by the registry, orchestrator, and client tests; a
    # non-terminating wired SSE can't be asserted through TestClient (it buffers to EOF).
    client = TestClient(create_app(EventBus()))
    resp = client.get("/coder/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data:" not in resp.text
