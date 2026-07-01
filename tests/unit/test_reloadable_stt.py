"""Tests for the reloadable STT proxy (live speech-model reload)."""

from __future__ import annotations

import numpy as np

from autobot.core.types import AudioClip, Segment, Transcription
from autobot.stt.reloadable import ReloadableSTT


class FakeSTT:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def transcribe(self, audio: AudioClip) -> Transcription:
        return Transcription(text=self.tag, confidence=1.0)

    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        if not self.tag:
            return []
        return [Segment(text=self.tag, start=0.0, end=1.0)]


_CLIP: AudioClip = np.zeros(16, dtype=np.float32)


def test_builds_lazily_on_first_transcribe() -> None:
    built = {"n": 0}

    def factory() -> FakeSTT:
        built["n"] += 1
        return FakeSTT("v1")

    stt = ReloadableSTT(factory)
    assert built["n"] == 0  # not loaded at construction (chat-first: no model load at startup)
    assert stt.transcribe(_CLIP).text == "v1"
    assert built["n"] == 1  # built on first use, then cached


def test_rebuilds_only_after_mark_dirty() -> None:
    versions = iter(["v1", "v2", "v3"])
    stt = ReloadableSTT(lambda: FakeSTT(next(versions)))
    assert stt.transcribe(_CLIP).text == "v1"  # eager build
    assert stt.transcribe(_CLIP).text == "v1"  # not dirty -> same instance
    stt.mark_dirty()
    assert stt.transcribe(_CLIP).text == "v2"  # reloaded once
    assert stt.transcribe(_CLIP).text == "v2"  # stays until next mark_dirty


def test_keeps_working_model_if_reload_fails() -> None:
    state = {"n": 0}

    def factory() -> FakeSTT:
        state["n"] += 1
        if state["n"] == 2:  # fail the reload, not the first build
            raise RuntimeError("bad model name")
        return FakeSTT(f"v{state['n']}")

    stt = ReloadableSTT(factory)
    assert stt.transcribe(_CLIP).text == "v1"
    stt.mark_dirty()
    assert stt.transcribe(_CLIP).text == "v1"  # reload failed -> kept v1
