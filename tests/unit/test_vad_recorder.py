"""Tests for the VAD-only recorder used by transcribe-then-match detection."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip
from autobot.io.listening import FRAME_SAMPLES, VadRecorder

_SPEECH = 0.8
_SILENCE = 0.0


def _frame(value: float) -> AudioClip:
    return np.full(FRAME_SAMPLES, value, dtype=np.float32)


class _ScriptedSource:
    def __init__(self, frames: list[AudioClip]) -> None:
        self._frames = frames
        self.closed = False

    def frames(self) -> Iterator[AudioClip]:
        yield from self._frames

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _VadFromSign:
    def speech_prob(self, frame: AudioClip) -> float:
        return 1.0 if float(frame[0]) > 0.0 else 0.0

    def reset(self) -> None:
        pass


def _recorder(frames: list[AudioClip]) -> VadRecorder:
    settings = Settings(input_mode="wake", vad_threshold=0.5, end_silence_ms=64)
    return VadRecorder(settings, _ScriptedSource(frames), _VadFromSign())


def test_captures_an_utterance_with_no_wake_word() -> None:
    # Leading silence, then speech, then trailing silence -> captures the phrase.
    frames = [
        _frame(_SILENCE),
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
    ]
    clip = _recorder(frames).record_clip()
    assert clip.size > 0


def test_returns_empty_if_only_silence() -> None:
    frames = [_frame(_SILENCE), _frame(_SILENCE), _frame(_SILENCE)]
    assert _recorder(frames).record_clip().size == 0


def test_close_releases_the_underlying_source() -> None:
    # Leaving voice mode must tear down the mic source so it stops capturing/ducking.
    source = _ScriptedSource([_frame(_SILENCE)])
    rec = VadRecorder(Settings(input_mode="wake"), source, _VadFromSign())
    rec.close()
    assert source.closed is True


def test_next_frame_resets_iterator_when_source_ends() -> None:
    # When the source ends (e.g. closed), the recorder drops its iterator so a later
    # call rebuilds it rather than returning None forever.
    rec = VadRecorder(Settings(input_mode="wake"), _ScriptedSource([]), _VadFromSign())
    assert rec._next_frame() is None  # source yielded nothing
    assert rec._frames is None  # iterator dropped, ready to rebuild


def _speech_frames() -> list[AudioClip]:
    return [
        _frame(_SILENCE),
        _frame(_SPEECH),
        _frame(_SPEECH),
        _frame(_SILENCE),
        _frame(_SILENCE),
    ]


class _AecSource(_ScriptedSource):
    aec_active = True


def test_vad_threshold_capped_for_aec_input() -> None:
    # AEC pre-cleans noise, so a high threshold must be capped or it never triggers.
    plain = VadRecorder(
        Settings(input_mode="wake", vad_threshold=0.8), _ScriptedSource([]), _VadFromSign()
    )
    assert plain._vad_threshold() == 0.8  # raw mic keeps the user's value
    aec = VadRecorder(
        Settings(input_mode="wake", vad_threshold=0.8), _AecSource([]), _VadFromSign()
    )
    assert aec._vad_threshold() == 0.5  # AEC mic capped at the ceiling
    low = VadRecorder(
        Settings(input_mode="wake", vad_threshold=0.4), _AecSource([]), _VadFromSign()
    )
    assert low._vad_threshold() == 0.4  # below the ceiling, AEC keeps it


def test_on_voice_signals_listening_while_awake() -> None:
    # The orb's "listening" animation is driven by this VAD signal — but only when
    # engaged: True when the user starts speaking, False when capture ends.
    events: list[bool] = []
    settings = Settings(input_mode="wake", vad_threshold=0.5, end_silence_ms=64)
    rec = VadRecorder(
        settings, _ScriptedSource(_speech_frames()), _VadFromSign(), on_voice=events.append
    )
    rec.set_awake(True)
    rec.record_clip()
    assert events == [True, False]


def test_on_voice_suppressed_when_not_awake() -> None:
    # Passive (not-addressed) capture must NOT light up the orb — otherwise ambient
    # speech animates "listening" and the orb never rests.
    events: list[bool] = []
    settings = Settings(input_mode="wake", vad_threshold=0.5, end_silence_ms=64)
    rec = VadRecorder(
        settings, _ScriptedSource(_speech_frames()), _VadFromSign(), on_voice=events.append
    )
    # default: not awake
    rec.record_clip()
    assert events == []


def test_reload_refreshes_endpointing_tunables_without_rebuild() -> None:
    # A reloader lets the Settings view tune the end-of-speech pause live.
    live = {"s": Settings(input_mode="wake", end_silence_ms=1600, max_utterance_s=40.0)}
    rec = VadRecorder(
        Settings(input_mode="wake", end_silence_ms=64),
        _ScriptedSource([_frame(_SILENCE)]),
        _VadFromSign(),
        reload=lambda: live["s"],
    )
    # Constructed from the live settings, not the (ignored) build-time ones.
    assert rec._end_silence_frames == round(1600 / 32.0)

    # Change the live settings; the next turn picks them up — no restart, no rebuild.
    live["s"] = Settings(input_mode="wake", end_silence_ms=320, max_utterance_s=20.0)
    rec.record_clip()
    assert rec._end_silence_frames == round(320 / 32.0)
    assert rec._max_frames == round(20.0 * 1000 / 32.0)
