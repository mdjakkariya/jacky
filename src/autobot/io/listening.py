"""Hands-free audio capture: wake word + VAD (Phase 2 :class:`AudioSource`).

The flow per :meth:`WakeWordVadRecorder.record_clip`:

1. flush any audio buffered while the previous turn was being processed, and
   reset the (stateful) wake/VAD models, so we always listen *live*;
2. stream mic frames, keeping a short pre-roll ring buffer, and score each with
   the wake-word model; once it crosses the threshold, start capturing;
3. run VAD on each captured frame and stop at the end-of-speech endpoint (or a
   hard maximum). If no speech actually follows the wake word, give up after a
   short timeout and return nothing — STT is gated strictly on detected speech,
   which also neutralizes Whisper's silence-hallucination.

The clip contract — mono ``float32`` at 16 kHz — is identical to push-to-talk, so
the orchestrator, STT, and gate are unchanged. The wake/VAD models and the mic
are injected (see :class:`FrameSource`), so the loop is tested with fakes.
"""

from __future__ import annotations

import queue
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip
from autobot.io.endpointing import FramePrebuffer, TrailingSilenceEndpointer, float_to_int16
from autobot.io.wake_vad import VoiceActivity, WakeDetector
from autobot.logging_setup import get_logger

_log = get_logger("listening")

FRAME_SAMPLES = 512
"""Frame size fed to the models: 512 samples = 32 ms at 16 kHz (silero's window)."""

_FRAME_MS = FRAME_SAMPLES / 16_000 * 1000  # 32.0
_PREROLL_FRAMES = 10  # ~320 ms of audio kept before the wake word fires
_START_FRAMES = 2  # frames of speech needed to consider the utterance started
_MAX_WAIT_FOR_SPEECH_S = 4.0  # give up if no speech follows the wake word


@runtime_checkable
class FrameSource(Protocol):
    """Yields fixed-size mono ``float32`` frames from some audio source."""

    def frames(self) -> Iterator[AudioClip]:
        """Yield 512-sample frames at 16 kHz, indefinitely."""
        ...

    def flush(self) -> None:
        """Discard any frames buffered since the last read (drop stale audio)."""
        ...


class MicFrameSource:
    """A persistent microphone stream yielding 512-sample frames via a queue.

    The stream is opened once and runs continuously; :meth:`flush` drops audio
    that accumulated while the assistant was busy, so each listen starts live.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue[AudioClip] = queue.Queue()
        self._started = False

    def _ensure_stream(self) -> None:
        if self._started:
            return
        import sounddevice as sd

        def callback(indata: AudioClip, _frames: int, _time: object, _status: object) -> None:
            self._queue.put(indata.reshape(-1).astype(np.float32).copy())

        stream = sd.InputStream(
            samplerate=self._settings.sample_rate,
            channels=self._settings.channels,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            callback=callback,
        )
        stream.start()
        self._stream = stream  # keep a reference so it isn't garbage-collected
        self._started = True

    def flush(self) -> None:
        """Drop everything currently queued (audio captured while we were busy)."""
        self._ensure_stream()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def frames(self) -> Iterator[AudioClip]:
        """Open the mic once and yield frames forever (one per audio block)."""
        self._ensure_stream()
        while True:
            yield self._queue.get()


class WakeWordVadRecorder:
    """Captures one utterance per call, triggered by a wake word and ended by VAD."""

    def __init__(
        self,
        settings: Settings,
        source: FrameSource,
        wake: WakeDetector,
        vad: VoiceActivity,
    ) -> None:
        self._settings = settings
        self._source = source
        self._wake = wake
        self._vad = vad
        self._frames: Iterator[AudioClip] | None = None
        self._end_silence_frames = max(1, round(settings.end_silence_ms / _FRAME_MS))
        self._max_frames = max(1, round(settings.max_utterance_s * 1000 / _FRAME_MS))
        self._max_wait_frames = max(1, round(_MAX_WAIT_FOR_SPEECH_S * 1000 / _FRAME_MS))

    def _next_frame(self) -> AudioClip | None:
        """Pull the next frame from the (persistent) source, or ``None`` if ended."""
        if self._frames is None:
            self._frames = iter(self._source.frames())
        return next(self._frames, None)

    def record_clip(self) -> AudioClip:
        """Wait for the wake word, capture the command, and return it; see module docs."""
        # Start each turn live: drop audio buffered while we were responding, and
        # clear the stateful models so they don't carry over the previous turn.
        self._source.flush()
        self._wake.reset()
        self._vad.reset()

        prebuffer = FramePrebuffer(_PREROLL_FRAMES)

        # Phase 1: idle until the wake word fires.
        print("[mic] Listening for the wake word…")
        while True:
            frame = self._next_frame()
            if frame is None:
                return np.zeros(0, dtype=np.float32)
            if self._wake.score(float_to_int16(frame)) >= self._settings.wake_threshold:
                break
            prebuffer.push(frame)

        # Phase 2: capture the command until VAD says speech ended.
        _log.info("wake detected model=%s", self._settings.wake_model)
        print("[mic] Wake word detected — listening for your command…")
        endpointer = TrailingSilenceEndpointer(
            speech_threshold=self._settings.vad_threshold,
            start_frames=_START_FRAMES,
            end_silence_frames=self._end_silence_frames,
        )
        collected: list[AudioClip] = prebuffer.drain()
        waited = 0
        while len(collected) < self._max_frames:
            frame = self._next_frame()
            if frame is None:
                break
            collected.append(frame)
            endpointer.update(self._vad.speech_prob(frame))
            if endpointer.finished:
                break
            if not endpointer.started:
                waited += 1
                if waited >= self._max_wait_frames:
                    break  # no speech after the wake word — bail out

        # Gate STT strictly on detected speech: if nothing was actually spoken,
        # return empty so we don't feed silence to Whisper (which hallucinates).
        if not endpointer.started:
            _log.info("no_speech_after_wake frames=%d", waited)
            print("[mic] No speech after wake word — ignoring.")
            return np.zeros(0, dtype=np.float32)

        audio: AudioClip = np.concatenate(collected).astype(np.float32)
        seconds = len(audio) / self._settings.sample_rate
        _log.info("captured seconds=%.1f frames=%d", seconds, len(collected))
        print(f"[mic] Captured {seconds:.1f}s of audio.")
        return audio
