"""The SSE frame generator serializes coder events to text/event-stream frames."""

from __future__ import annotations

from autobot.daemon.server import _sse_frames


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
