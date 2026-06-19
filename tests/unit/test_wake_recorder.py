"""Tests for the wake-word + VAD recorder loop, using fake models and frames.

No microphone or ML runtime is involved: a scripted frame source plus fake
wake/VAD scorers let us assert the capture logic deterministically.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip, Int16Frame
from autobot.io.listening import FRAME_SAMPLES, WakeWordVadRecorder


def _frame(value: float) -> AudioClip:
    return np.full(FRAME_SAMPLES, value, dtype=np.float32)


class _ScriptedSource:
    """Yields a fixed list of frames, then stops."""

    def __init__(self, frames: list[AudioClip]) -> None:
        self._frames = frames

    def frames(self) -> Iterator[AudioClip]:
        yield from self._frames

    def flush(self) -> None:
        pass


class _WakeOnValue:
    """Fires the wake word when a frame's first sample is non-zero."""

    def __init__(self, trigger: float) -> None:
        self._trigger = trigger

    def score(self, frame_int16: Int16Frame) -> float:
        return 1.0 if frame_int16[0] != 0 else 0.0

    def reset(self) -> None:
        pass


class _VadFromSign:
    """Treats a positive-valued frame as speech, zero as silence."""

    def speech_prob(self, frame: AudioClip) -> float:
        return 1.0 if float(frame[0]) > 0.0 else 0.0

    def reset(self) -> None:
        pass


def _settings() -> Settings:
    # end_silence_ms=64 -> 2 frames of silence (32 ms each) ends the utterance.
    return Settings(input_mode="wake", wake_threshold=0.5, vad_threshold=0.5, end_silence_ms=64)


def test_waits_for_wake_then_captures_until_silence() -> None:
    frames = [
        _frame(0.0),  # pre-wake silence (goes to pre-roll)
        _frame(0.9),  # WAKE fires on this frame
        _frame(0.8),  # speech
        _frame(0.8),  # speech
        _frame(0.0),  # silence 1
        _frame(0.0),  # silence 2 -> endpoint
        _frame(0.7),  # should never be read
    ]
    rec = WakeWordVadRecorder(
        _settings(), _ScriptedSource(frames), _WakeOnValue(0.9), _VadFromSign()
    )
    clip = rec.record_clip()
    # Pre-roll (1 frame) + 4 command frames (the wake frame itself isn't captured;
    # frames 2,3 speech then 4,5 silence -> endpoint) = 5 frames.
    assert clip.size == 5 * FRAME_SAMPLES


def test_includes_preroll_so_first_syllable_not_clipped() -> None:
    # 2 pre-roll (silence) frames, wake, then real speech then trailing silence.
    frames = [
        _frame(0.0),
        _frame(0.0),
        _frame(0.9),
        _frame(0.8),
        _frame(0.8),
        _frame(0.0),
        _frame(0.0),
    ]
    rec = WakeWordVadRecorder(
        _settings(), _ScriptedSource(frames), _WakeOnValue(0.9), _VadFromSign()
    )
    clip = rec.record_clip()
    # Pre-roll (2) + 4 command frames (speech x2, silence x2 -> endpoint) = 6.
    assert clip.size == 6 * FRAME_SAMPLES
    # The two pre-roll frames (silence) are prepended ahead of the speech.
    assert np.all(clip[: 2 * FRAME_SAMPLES] == 0)


def test_returns_empty_if_stream_ends_before_wake() -> None:
    frames = [_frame(0.0), _frame(0.0)]
    rec = WakeWordVadRecorder(
        _settings(), _ScriptedSource(frames), _WakeOnValue(0.9), _VadFromSign()
    )
    assert rec.record_clip().size == 0


def test_no_speech_after_wake_returns_empty() -> None:
    # Wake fires, but only silence follows -> STT must be gated (empty clip),
    # not 15s of silence handed to Whisper.
    frames = [_frame(0.9), _frame(0.0), _frame(0.0), _frame(0.0)]
    rec = WakeWordVadRecorder(
        _settings(), _ScriptedSource(frames), _WakeOnValue(0.9), _VadFromSign()
    )
    assert rec.record_clip().size == 0


def test_caps_at_max_utterance_length() -> None:
    # Wake immediately, then unbroken speech: must stop at max_utterance_s.
    settings = Settings(input_mode="wake", end_silence_ms=64, max_utterance_s=0.16)  # ~5 frames
    frames = [_frame(0.9)] + [_frame(0.8) for _ in range(50)]
    rec = WakeWordVadRecorder(settings, _ScriptedSource(frames), _WakeOnValue(0.9), _VadFromSign())
    clip = rec.record_clip()
    max_frames = round(0.16 * 1000 / (FRAME_SAMPLES / 16_000 * 1000))
    assert clip.size == max_frames * FRAME_SAMPLES
