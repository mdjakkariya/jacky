"""Tests for the lazy voice-I/O proxy (chat-first: no mic/model until voice is used)."""

from __future__ import annotations

from typing import Any

from autobot.io.lazy_voice import LazyVoiceIO


class _FakeAudio:
    aec_active = True

    def __init__(self) -> None:
        self.last_speech_started_at = 1.0
        self.clips = 0
        self.closed = False

    def record_clip(self) -> str:
        self.clips += 1
        return "clip"

    def record_continuation(self, timeout_s: float) -> str:
        return "cont"

    def set_awake(self, awake: bool) -> None: ...

    def close(self) -> None:
        self.closed = True


class _FakeTTS:
    def __init__(self) -> None:
        self.said: list[str] = []

    def speak(self, text: str) -> None:
        self.said.append(text)

    def stop(self) -> None: ...


def _holder() -> tuple[LazyVoiceIO, dict[str, Any]]:
    built: dict[str, Any] = {"n": 0, "audios": []}

    def factory() -> tuple[Any, Any]:
        built["n"] += 1
        audio = _FakeAudio()
        built["audios"].append(audio)
        return audio, _FakeTTS()

    return LazyVoiceIO(factory), built


def test_not_built_until_first_real_use() -> None:
    io, built = _holder()
    audio: Any = io.audio
    # Holding proxies and probing optional methods/attrs must NOT build the mic.
    assert callable(audio.record_continuation)  # confirmer probes this
    assert getattr(audio, "aec_active", False) is False  # absent until built — no build
    assert built["n"] == 0


def test_record_continuation_no_arg_uses_default_window() -> None:
    # Regression: the proxy must keep the recorder's default so the orchestrator's
    # cut-off re-open (which calls cont() with no argument) doesn't crash the turn.
    io, built = _holder()
    audio: Any = io.audio  # record_continuation is an optional, getattr-probed method
    assert audio.record_continuation() == "cont"
    assert built["n"] == 1


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


def test_release_before_build_is_a_noop() -> None:
    io, built = _holder()
    io.release()  # nothing built yet -> must not raise or build
    assert built["n"] == 0


def test_release_closes_source_and_rebuilds_on_next_use() -> None:
    io, built = _holder()
    assert io.audio.record_clip() == "clip"  # builds once
    assert built["n"] == 1

    io.release()  # tears down: closes the source, drops the built I/O
    assert built["audios"][0].closed is True

    # The next use rebuilds a *fresh* mic/tts (so a finicky duplex unit isn't restarted
    # in place) — this is what reopens the mic when switching back to voice.
    assert io.audio.record_clip() == "clip"
    assert built["n"] == 2
    assert built["audios"][1].closed is False
