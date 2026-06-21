"""Tests for the per-session transcript."""

from __future__ import annotations

from pathlib import Path

from autobot.session_log import FileTranscript, NullTranscript


def test_file_transcript_records_conversation(tmp_path: Path) -> None:
    t = FileTranscript(tmp_path, header="model: test")
    t.user("what time is it", 0.82)
    t.tool("get_time", {}, ok=True, detail="Friday")
    t.assistant("It's Friday afternoon.")
    t.note("1200/4096 tokens (29%)")
    t.close()

    assert t.path is not None
    text = t.path.read_text(encoding="utf-8")
    assert "model: test" in text
    assert "what time is it" in text
    assert "get_time" in text
    assert "It's Friday afternoon." in text
    assert "1200/4096 tokens" in text
    assert "session ended" in text


def test_records_cloud_usage_and_session_total(tmp_path: Path) -> None:
    t = FileTranscript(tmp_path)
    t.user("hi", 0.9)
    t.record_usage(1000, 50, 0.00125)
    t.record_usage(1200, 40, 0.00140)
    t.close()

    assert t.path is not None
    text = t.path.read_text(encoding="utf-8")
    # Per-turn usage notes appear in the body.
    assert "context 1,000 tok" in text and "output 50 tok" in text
    # The footer carries the session totals: 2 requests, 2,290 total tokens.
    assert "Cloud usage this session" in text
    assert "2 request(s)" in text
    assert "total 2,290 tok" in text
    assert "est. cost ~$0.0027" in text  # 0.00125 + 0.00140 rounded to 4 dp


def test_no_usage_footer_for_local_only_session(tmp_path: Path) -> None:
    # A local session never calls record_usage -> no cost block (cloud only).
    t = FileTranscript(tmp_path)
    t.user("hi", 0.9)
    t.assistant("hello")
    t.close()
    assert t.path is not None
    assert "Cloud usage this session" not in t.path.read_text(encoding="utf-8")


def test_usage_without_cost_omits_dollar_estimate(tmp_path: Path) -> None:
    # Unknown model -> cost is None -> tokens shown, no dollar figure.
    t = FileTranscript(tmp_path)
    t.record_usage(500, 20, None)
    t.close()
    assert t.path is not None
    text = t.path.read_text(encoding="utf-8")
    assert "Cloud usage this session" in text
    assert "est. cost" not in text


def test_file_transcript_filename_and_location(tmp_path: Path) -> None:
    t = FileTranscript(tmp_path)
    assert t.path is not None
    assert t.path.parent == tmp_path
    assert t.path.name.startswith("session-") and t.path.suffix == ".md"
    t.close()


def test_null_transcript_is_noop() -> None:
    t = NullTranscript()
    t.user("hi", 1.0)
    t.assistant("hello")
    t.tool("x", {}, True, "")
    t.note("n")
    t.record_usage(100, 10, 0.001)
    t.close()
    assert t.path is None
