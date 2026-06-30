from __future__ import annotations

from autobot.stt.whisper_cpp_stt import segments_from_cpp


class _Seg:
    def __init__(self, text: str, t0: int, t1: int) -> None:
        self.text, self.t0, self.t1 = text, t0, t1  # t0/t1 in centiseconds


def test_centisecond_timing_objects() -> None:
    segs = segments_from_cpp([_Seg(" hi ", 0, 50), _Seg("  ", 50, 60), _Seg("there", 60, 130)])
    assert [(s.text, s.start, s.end) for s in segs] == [("hi", 0.0, 0.5), ("there", 0.6, 1.3)]


def test_dict_seconds_timing() -> None:
    segs = segments_from_cpp([{"text": "yo", "start": 1.0, "end": 2.0}])
    assert [(s.text, s.start, s.end) for s in segs] == [("yo", 1.0, 2.0)]


def test_empty() -> None:
    assert segments_from_cpp(None) == []
