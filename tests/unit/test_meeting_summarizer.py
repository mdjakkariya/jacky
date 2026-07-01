from __future__ import annotations

from autobot.meeting.summarizer import MeetingSummarizer, batch_notes, chunk_text


def test_chunk_text_respects_max() -> None:
    text = "\n".join(f"line {i}" for i in range(20))
    chunks = chunk_text(text, max_chars=30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_batch_notes_groups_whole_notes() -> None:
    notes = ["aaaa", "bbbb", "cccc"]  # 4 chars each; +2 join sep
    batches = batch_notes(notes, max_chars=10)  # "aaaa\n\nbbbb"=10 fits, +cccc overflows
    assert batches == ["aaaa\n\nbbbb", "cccc"]


def test_oversized_note_is_its_own_batch() -> None:
    assert batch_notes(["x" * 50, "y" * 50], max_chars=10) == ["x" * 50, "y" * 50]


def test_map_reduce_calls_completer_per_chunk_then_reduces() -> None:
    calls: list[str] = []

    def fake_complete(prompt: str) -> str:
        calls.append(prompt)
        return f"NOTE({len(calls)})"

    big = "\n".join(f"sentence number {i}" for i in range(50))
    s = MeetingSummarizer(fake_complete, max_chars=80)
    out = s.summarize(big, title="Standup", date="2026-06-30", duration="12m", mic_only=False)
    assert "Standup" in out and "2026-06-30" in out
    assert len(calls) >= 2


def test_recurses_and_terminates_when_notes_never_shrink() -> None:
    # Completer always returns 200 chars > max_chars=100 (never shrinks). Must still
    # terminate (no infinite recursion / memory blowup) and return a string.
    s = MeetingSummarizer(lambda p: "x" * 200, max_chars=100)
    out = s.summarize("a\n" * 300, title="T", date="D", duration="1m", mic_only=False)
    assert out
