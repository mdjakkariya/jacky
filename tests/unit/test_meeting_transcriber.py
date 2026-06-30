"""Tests for meeting transcriber (windowing, dedupe, merge)."""

from __future__ import annotations

from autobot.core.types import Segment
from autobot.meeting.transcriber import (
    dedupe_overlap,
    merge_streams,
    plan_windows,
    render_transcript,
)


def test_plan_windows_overlap() -> None:
    assert plan_windows(70.0, 30.0, 3.0) == [(0.0, 30.0), (27.0, 57.0), (54.0, 70.0)]


def test_plan_windows_short_audio() -> None:
    assert plan_windows(12.0, 30.0, 3.0) == [(0.0, 12.0)]


def test_dedupe_overlap_drops_repeated_boundary_word() -> None:
    segs = [
        Segment("hello there", 0.0, 2.0),
        Segment("hello there", 1.9, 2.1),
        Segment("next", 5.0, 6.0),
    ]
    out = dedupe_overlap(segs)
    assert [s.text for s in out] == ["hello there", "next"]


def test_merge_tags_and_orders() -> None:
    near = [Segment("hi", 0.0, 1.0), Segment("bye", 5.0, 6.0)]
    far = [Segment("hello", 2.0, 3.0)]
    lines = merge_streams(near, far)
    assert [(who, s.text) for who, s in lines] == [
        ("you", "hi"),
        ("participants", "hello"),
        ("you", "bye"),
    ]


def test_render_marks_mic_only() -> None:
    out = render_transcript([("you", Segment("hi", 0.0, 1.0))], mic_only=True)
    assert "[you]" in out and "mic-only" in out.lower()
