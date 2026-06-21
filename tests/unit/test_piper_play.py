"""Tests for the cancellable TTS playback helper (no audio device, no piper)."""

from __future__ import annotations

import threading

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
