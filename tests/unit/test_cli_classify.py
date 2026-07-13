"""classify() maps daemon status dicts to semantic Segments."""

from __future__ import annotations

from autobot.cli.classify import Segment, classify


def test_plan_segment_carries_reply_and_todo() -> None:
    seg = classify({"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]})
    assert seg == Segment("plan", "1. edit foo", ("edit foo",))


def test_pending_uses_prompt() -> None:
    seg = classify({"status": "pending", "kind": "command", "prompt": "Run `pytest`?"})
    assert seg.kind == "pending" and seg.text == "Run `pytest`?"


def test_done_uses_reply() -> None:
    assert classify({"status": "done", "reply": "Done."}) == Segment("done", "Done.", ())


def test_error_uses_reply() -> None:
    assert classify({"status": "error", "reply": "boom"}).kind == "error"


def test_unknown_status_falls_back_to_done() -> None:
    assert classify({"reply": "hi"}).kind == "done"


def test_classify_token_event() -> None:
    seg = classify({"type": "token", "text": "hel"})
    assert seg.kind == "token" and seg.text == "hel"


def test_classify_tool_event() -> None:
    seg = classify({"type": "tool", "event": "start", "name": "read_file", "label": "Read a.py"})
    assert seg.kind == "tool" and "Read a.py" in seg.text
