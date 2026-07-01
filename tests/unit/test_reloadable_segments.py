"""Tests for ReloadableSTT.transcribe_segments forwarding with lazy rebuild."""

from __future__ import annotations

import numpy as np

from autobot.core.types import Segment, Transcription
from autobot.stt.reloadable import ReloadableSTT


class _FakeSTT:
    def transcribe(self, audio):  # type: ignore[no-untyped-def]
        return Transcription(text="", confidence=0.0)

    def transcribe_segments(self, audio, **kw):  # type: ignore[no-untyped-def]
        return [Segment(text="ok", start=0.0, end=1.0)]


def test_forwards_and_lazy_builds() -> None:
    built = {"n": 0}

    def factory():  # type: ignore[no-untyped-def]
        built["n"] += 1
        return _FakeSTT()

    stt = ReloadableSTT(factory)
    out = stt.transcribe_segments(np.zeros(16000, dtype=np.float32), vad_filter=True)
    assert [s.text for s in out] == ["ok"]
    assert built["n"] == 1  # built lazily, once
