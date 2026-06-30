from __future__ import annotations

import numpy as np

from autobot.core.interfaces import SpeechToText
from autobot.core.types import Segment, Transcription


def test_segment_is_frozen_value_object() -> None:
    seg = Segment(text="hello", start=0.0, end=1.5)
    assert (seg.text, seg.start, seg.end) == ("hello", 0.0, 1.5)
    import pytest

    with pytest.raises(AttributeError):
        seg.text = "x"  # type: ignore[misc]


def test_fake_with_transcribe_segments_satisfies_protocol() -> None:
    class Fake:
        def transcribe(self, audio: np.ndarray) -> Transcription:
            return Transcription(text="", confidence=0.0)

        def transcribe_segments(
            self,
            audio: np.ndarray,
            *,
            language: str = "en",
            vad_filter: bool = True,
            condition_on_previous_text: bool = False,
            initial_prompt: str | None = None,
        ) -> list[Segment]:
            return [Segment(text="hi", start=0.0, end=0.4)]

    assert isinstance(Fake(), SpeechToText)
