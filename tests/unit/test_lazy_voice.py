"""Tests for the lazy voice-I/O proxy (chat-first: no mic/model until voice is used)."""

from __future__ import annotations

from typing import Any

from autobot.io.lazy_voice import LazyVoiceIO


class _FakeAudio:
    aec_active = True

    def __init__(self) -> None:
        self.last_speech_started_at = 1.0
        self.clips = 0

    def record_clip(self) -> str:
        self.clips += 1
        return "clip"

    def record_continuation(self, timeout_s: float) -> str:
        return "cont"

    def set_awake(self, awake: bool) -> None: ...


class _FakeTTS:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str) -> None:
        self.said.append(text)

    def stop(self) -> None: ...


def _holder() -> tuple[LazyVoiceIO, dict[str, int]]:
    built = {"n": 0}

    def factory() -> tuple[Any, Any]:
        built["n"] += 1
        return _FakeAudio(), _FakeTTS()

    return LazyVoiceIO(factory), built


def test_not_built_until_first_real_use() -> None:
    io, built = _holder()
    audio: Any = io.audio
    # Holding proxies and probing optional methods/attrs must NOT build the mic.
    assert callable(audio.record_continuation)  # confirmer probes this
    assert getattr(audio, "aec_active", False) is False  # absent until built — no build
    assert built["n"] == 0


def test_first_record_or_speak_builds_once() -> None:
    io, built = _holder()
    assert io.audio.record_clip() == "clip"
    assert built["n"] == 1
    io.tts.speak("hi")  # shares the same built I/O
    assert built["n"] == 1  # not rebuilt


def test_attribute_reads_delegate_after_build() -> None:
    io, _ = _holder()
    audio: Any = io.audio
    audio.record_clip()  # forces the build
    assert audio.last_speech_started_at == 1.0  # now delegates to the real audio
    assert audio.aec_active is True


def test_stop_before_build_is_a_noop() -> None:
    io, built = _holder()
    io.tts.stop()  # nothing built yet -> must not raise or build
    assert built["n"] == 0
