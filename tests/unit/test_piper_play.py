"""Tests for the cancellable TTS playback helper (no audio device, no piper)."""

from __future__ import annotations

import threading
from typing import Any, cast

import numpy as np

from autobot.core.types import AudioClip
from autobot.tts.piper_tts import play_cancellable


class _FakeStream:
    def __init__(self, on_write: object = None) -> None:
        self.written = 0
        self.started = False
        self.stopped = False
        self.aborted = False
        self._on_write = on_write

    def start(self) -> None:
        self.started = True

    def write(self, chunk: AudioClip) -> None:
        self.written += int(chunk.size)
        if callable(self._on_write):
            self._on_write()

    def stop(self) -> None:
        self.stopped = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        pass


class _FakeSd:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def OutputStream(self, **_kwargs: object) -> _FakeStream:  # noqa: N802 - mimics sounddevice
        return self._stream


class _FakePlayer:
    def __init__(self) -> None:
        self.calls: list[tuple[AudioClip, int]] = []

    def play(
        self, audio: AudioClip, sample_rate: int, cancel: object, on_level: object = None
    ) -> bool:
        self.calls.append((audio, sample_rate))
        return True


class _Chunk:
    def __init__(self, arr: AudioClip, rate: int) -> None:
        self.audio_int16_array = arr
        self.sample_rate = rate


class _FakeVoice:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def synthesize(self, _text: str) -> object:
        return iter(self._chunks)


def test_pipertts_routes_audio_to_injected_player() -> None:
    from autobot.tts.piper_tts import AudioPlayer, PiperTTS, SoundDevicePlayer

    tts = object.__new__(PiperTTS)  # bypass __init__ (no piper / no model file)
    # cast(Any, …) so the fakes assign cleanly whether or not piper is installed —
    # its absence makes _voice's declared type Any on CI, which would otherwise make
    # a `type: ignore` here "unused".
    chunks = [
        _Chunk(np.array([1, 2, 3], dtype=np.int16), 22_050),
        _Chunk(np.array([4, 5], dtype=np.int16), 22_050),
    ]
    player = _FakePlayer()
    tts._voice = cast(Any, _FakeVoice(chunks))
    tts._player = cast(Any, player)
    tts._on_level = None
    tts._cancel = threading.Event()

    tts.speak("hello")

    assert len(player.calls) == 1
    audio, rate = player.calls[0]
    assert rate == 22_050
    assert list(audio) == [1, 2, 3, 4, 5]  # chunks concatenated
    assert isinstance(player, AudioPlayer)  # conforms to the protocol
    assert isinstance(SoundDevicePlayer(), AudioPlayer)


def test_strip_for_speech_removes_emoji_but_keeps_words() -> None:
    from autobot.tts.piper_tts import _strip_for_speech

    assert _strip_for_speech("🗑️ Empty the Trash?") == "Empty the Trash?"
    assert _strip_for_speech("🔐 Grant access ✅") == "Grant access"
    assert _strip_for_speech("plain text") == "plain text"
    # An emoji-only string speaks nothing (so speak() short-circuits).
    assert _strip_for_speech("👍🎉") == ""


def test_speak_strips_emoji_before_synthesis() -> None:
    from autobot.tts.piper_tts import PiperTTS

    seen: list[str] = []

    class _RecordingVoice:
        def synthesize(self, text: str) -> object:
            seen.append(text)
            return iter([_Chunk(np.array([1], dtype=np.int16), 22_050)])

    tts = object.__new__(PiperTTS)
    tts._voice = cast(Any, _RecordingVoice())
    tts._player = cast(Any, _FakePlayer())
    tts._on_level = None
    tts._cancel = threading.Event()

    tts.speak("🗑️ Empty the Trash?")
    assert seen == ["Empty the Trash?"]  # emoji never reached the synthesizer


def test_plays_to_completion_when_not_cancelled() -> None:
    audio = np.zeros(16_000, dtype=np.int16)  # 1s @ 16k
    stream = _FakeStream()
    cancel = threading.Event()
    completed = play_cancellable(_FakeSd(stream), audio, 16_000, cancel)
    assert completed is True
    assert stream.written == audio.size  # every block written
    assert stream.stopped and not stream.aborted


def test_aborts_immediately_when_cancelled_up_front() -> None:
    audio = np.zeros(16_000, dtype=np.int16)
    stream = _FakeStream()
    cancel = threading.Event()
    cancel.set()  # already interrupted before the first block
    completed = play_cancellable(_FakeSd(stream), audio, 16_000, cancel)
    assert completed is False
    assert stream.written == 0
    assert stream.aborted and not stream.stopped


def test_stops_mid_playback_when_cancelled_partway() -> None:
    audio = np.zeros(16_000, dtype=np.int16)
    cancel = threading.Event()
    state = {"writes": 0}

    def after_write() -> None:
        state["writes"] += 1
        if state["writes"] == 2:  # user barges in after two blocks
            cancel.set()

    stream = _FakeStream(on_write=after_write)
    completed = play_cancellable(_FakeSd(stream), audio, 16_000, cancel)
    assert completed is False
    assert stream.aborted
    block = 16_000 // 30
    assert stream.written == 2 * block  # stopped right after the 2nd block
