"""Tests for the wake-word + VAD recorder loop, using fake models and frames.

No microphone or ML runtime is involved: a scripted frame source plus fake
wake/VAD scorers let us assert the capture logic deterministically.

Convention for the fakes below:
* a frame value of ``0.95`` is the *wake word* (only it scores as a wake);
* ``0.8`` is ordinary *speech* (VAD = speech, but NOT a wake);
* ``0.0`` is *silence*.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip, Int16Frame
from autobot.io.listening import FRAME_SAMPLES, WakeWordVadRecorder

_WAKE = 0.95
_SPEECH = 0.8
_SILENCE = 0.0


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


class _WakeOnLoudFrame:
    """Fires the wake word only for the distinct wake marker (~0.95 -> int16 high)."""

    def score(self, frame_int16: Int16Frame) -> float:
        return 1.0 if frame_int16[0] >= 30000 else 0.0

    def reset(self) -> None:
        pass


class _VadFromSign:
    """Treats any positive-valued frame as speech, zero as silence."""

    def speech_prob(self, frame: AudioClip) -> float:
        return 1.0 if float(frame[0]) > 0.0 else 0.0

    def reset(self) -> None:
        pass


def _settings(**overrides: object) -> Settings:
    # end_silence_ms=64 -> 2 frames of silence (32 ms each) ends the utterance.
    base: dict[str, object] = {
        "input_mode": "wake",
        "wake_threshold": 0.5,
        "vad_threshold": 0.5,
        "end_silence_ms": 64,
        "wake_preroll_ms": 0,  # off by default in tests; one test opts in
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _recorder(frames: list[AudioClip], **settings: object) -> WakeWordVadRecorder:
    return WakeWordVadRecorder(
        _settings(**settings), _ScriptedSource(frames), _WakeOnLoudFrame(), _VadFromSign()
    )


def test_waits_for_wake_then_captures_until_silence() -> None:
    frames = [
        _frame(_SILENCE),  # pre-wake silence (ignored, before wake)
        _frame(_WAKE),  # WAKE fires here
        _frame(_SPEECH),  # speech (start)
        _frame(_SPEECH),  # speech (started)
        _frame(_SILENCE),  # silence 1
        _frame(_SILENCE),  # silence 2 -> endpoint
        _frame(_SPEECH),  # never read
    ]
    clip = _recorder(frames).record_clip()
    # After wake: speech x2 (pre-roll+start) then silence x2 -> 4 frames.
    assert clip.size == 4 * FRAME_SAMPLES


def test_preroll_keeps_silence_before_speech_so_first_syllable_not_clipped() -> None:
    frames = [
        _frame(_WAKE),  # wake
        _frame(_SILENCE),  # pre-roll silence (kept)
        _frame(_SILENCE),  # pre-roll silence (kept)
        _frame(_SPEECH),  # speech
        _frame(_SPEECH),  # speech (started)
        _frame(_SILENCE),  # silence 1
        _frame(_SILENCE),  # silence 2 -> endpoint
    ]
    clip = _recorder(frames).record_clip()
    assert clip.size == 6 * FRAME_SAMPLES
    # The two pre-roll silence frames are prepended ahead of the speech.
    assert np.all(clip[: 2 * FRAME_SAMPLES] == 0)


def test_returns_empty_if_stream_ends_before_wake() -> None:
    assert _recorder([_frame(_SILENCE), _frame(_SILENCE)]).record_clip().size == 0


def test_no_speech_after_wake_returns_empty() -> None:
    # Wake fires, only silence follows -> gated empty, not silence to Whisper.
    clip = _recorder([_frame(_WAKE), _frame(_SILENCE), _frame(_SILENCE), _frame(_SILENCE)])
    assert clip.record_clip().size == 0


def test_caps_at_max_utterance_length() -> None:
    frames = [_frame(_WAKE)] + [_frame(_SPEECH) for _ in range(50)]
    clip = _recorder(frames, max_utterance_s=0.16).record_clip()  # ~5 frames
    max_frames = round(0.16 * 1000 / (FRAME_SAMPLES / 16_000 * 1000))
    assert clip.size == max_frames * FRAME_SAMPLES


def test_continuous_command_recovered_via_wake_preroll() -> None:
    # Models "hey jarvis what…" said in one breath: the command onset (speech)
    # arrives just before the wake word fires. Without pre-roll it would be lost.
    frames = [
        _frame(_SPEECH),  # command onset, before wake fires (in pre-roll)
        _frame(_SPEECH),
        _frame(_WAKE),  # wake fires here (late, mid-utterance)
        _frame(_SPEECH),  # rest of the command
        _frame(_SILENCE),
        _frame(_SILENCE),  # endpoint
    ]
    # Without pre-roll the onset is dropped and only one post-wake speech frame
    # remains — not enough to start -> empty.
    assert _recorder(list(frames), wake_preroll_ms=0).record_clip().size == 0
    # With pre-roll (~96 ms = 3 frames) the onset is recovered and captured.
    assert _recorder(list(frames), wake_preroll_ms=96).record_clip().size > 0


def test_follow_up_skips_wake_after_a_turn() -> None:
    # Turn 1 needs the wake word; turn 2 is captured WITHOUT it (follow-up window).
    frames = [
        _frame(_WAKE),
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
        # turn 2: no wake marker, just speech
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
    ]
    rec = _recorder(frames, follow_up_window_s=1.0)
    assert rec.record_clip().size == 4 * FRAME_SAMPLES  # turn 1 (via wake)
    assert rec.record_clip().size == 4 * FRAME_SAMPLES  # turn 2 (no wake!)


def test_follow_up_times_out_then_requires_wake_again() -> None:
    frames = [
        _frame(_WAKE),
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
        # follow-up window: only silence -> times out, re-arms wake
        _frame(_SILENCE),
        _frame(_SILENCE),
        # next real turn must use the wake word again
        _frame(_WAKE),
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
    ]
    # follow_up_window_s=0.064 -> 2 frames before the window lapses.
    rec = _recorder(frames, follow_up_window_s=0.064)
    assert rec.record_clip().size == 4 * FRAME_SAMPLES  # turn 1
    assert rec.record_clip().size == 4 * FRAME_SAMPLES  # follow-up times out, wake works
