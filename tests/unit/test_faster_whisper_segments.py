from __future__ import annotations

from dataclasses import dataclass

from autobot.stt.faster_whisper_stt import segments_from_faster_whisper


@dataclass
class _Raw:
    text: str
    start: float
    end: float


def test_maps_and_strips_and_drops_empty() -> None:
    raw = [_Raw("  hello ", 0.0, 1.0), _Raw("   ", 1.0, 1.2), _Raw("world", 1.2, 2.0)]
    segs = segments_from_faster_whisper(raw)
    assert [(s.text, s.start, s.end) for s in segs] == [("hello", 0.0, 1.0), ("world", 1.2, 2.0)]


def test_empty_input() -> None:
    assert segments_from_faster_whisper([]) == []
