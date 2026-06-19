"""Hands-free audio capture: wake word + VAD (Phase 2 :class:`AudioSource`).

The flow per :meth:`WakeWordVadRecorder.record_clip`:

1. flush any audio buffered while the previous turn was being processed, and
   reset the (stateful) wake/VAD models, so we always listen *live*;
2. **conversational follow-up:** right after a turn, listen for the next command
   *without* the wake word for ``follow_up_window_s``; if the user speaks, capture
   it and stay open. If the window passes in silence, re-arm the wake word — like
   a natural back-and-forth that lapses once you've stopped talking;
3. otherwise stream mic frames and score each with the wake-word model; once it
   crosses the threshold, start capturing;
4. run VAD on each captured frame and stop at the end-of-speech endpoint (or a
   hard maximum). If no speech actually follows, give up after a timeout and
   return nothing — STT is gated strictly on detected speech, which also
   neutralizes Whisper's silence-hallucination.

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
        self._follow_up_frames = max(0, round(settings.follow_up_window_s * 1000 / _FRAME_MS))
        # After a turn we stay "open" for a follow-up without the wake word.
        self._follow_up_active = False

    def _next_frame(self) -> AudioClip | None:
        """Pull the next frame from the (persistent) source, or ``None`` if ended."""
        if self._frames is None:
            self._frames = iter(self._source.frames())
        return next(self._frames, None)

    def _capture(self, wait_frames: int) -> AudioClip | None:
        """Capture one utterance, waiting up to ``wait_frames`` for speech to start.

        Keeps a rolling pre-roll while waiting, so the first syllable isn't
        clipped. Returns the clip, or ``None`` if no speech started within the
        window (so STT is never fed silence — Whisper hallucinates on it).
        """
        endpointer = TrailingSilenceEndpointer(
            speech_threshold=self._settings.vad_threshold,
            start_frames=_START_FRAMES,
            end_silence_frames=self._end_silence_frames,
        )
        prebuffer = FramePrebuffer(_PREROLL_FRAMES)
        collected: list[AudioClip] | None = None
        waited = 0

        while True:
            frame = self._next_frame()
            if frame is None:
                break
            endpointer.update(self._vad.speech_prob(frame))
            if collected is None:
                # Still waiting for speech to begin: keep a rolling pre-roll.
                prebuffer.push(frame)
                if endpointer.started:
                    collected = prebuffer.drain()  # include the pre-roll lead-in
                else:
                    waited += 1
                    if waited >= wait_frames:
                        return None
            else:
                collected.append(frame)
                if endpointer.finished or len(collected) >= self._max_frames:
                    break

        if collected is None or not endpointer.started:
            return None
        return np.concatenate(collected).astype(np.float32)

    def record_clip(self) -> AudioClip:
        """Capture one turn: a follow-up if still in conversation, else wake-word first.

        See the module docstring for the full flow.
        """
        # Start each turn live: drop audio buffered while we were responding, and
        # clear the stateful models so they don't carry over the previous turn.
        self._source.flush()
        self._wake.reset()
        self._vad.reset()

        # Conversational follow-up: listen for the next command without the wake
        # word for a while. If the user speaks, capture it and stay open.
        if self._follow_up_active and self._follow_up_frames > 0:
            print("[mic] Listening for a follow-up… (or stay quiet to end)")
            clip = self._capture(self._follow_up_frames)
            if clip is not None:
                seconds = clip.size / self._settings.sample_rate
                _log.info("follow_up captured seconds=%.1f", seconds)
                print(f"[mic] Captured {seconds:.1f}s of audio.")
                return clip
            self._follow_up_active = False
            _log.info("follow_up window ended — re-arming wake word")
            print("[mic] Follow-up window ended.")

        # Idle until the wake word fires.
        print("[mic] Listening for the wake word…")
        while True:
            frame = self._next_frame()
            if frame is None:
                return np.zeros(0, dtype=np.float32)
            if self._wake.score(float_to_int16(frame)) >= self._settings.wake_threshold:
                break

        _log.info("wake detected model=%s", self._settings.wake_model)
        print("[mic] Wake word detected — listening for your command…")
        clip = self._capture(self._max_wait_frames)
        if clip is None:
            _log.info("no_speech_after_wake")
            print("[mic] No speech after wake word — ignoring.")
            return np.zeros(0, dtype=np.float32)

        # A successful turn opens the follow-up window for the next call.
        self._follow_up_active = True
        seconds = clip.size / self._settings.sample_rate
        _log.info("captured seconds=%.1f", seconds)
        print(f"[mic] Captured {seconds:.1f}s of audio.")
        return clip
