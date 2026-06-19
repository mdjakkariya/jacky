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
    t.close()
    assert t.path is None
