"""macOS Voice-Processing I/O microphone — echo-cancelled capture for barge-in.

Drop-in replacement for :class:`~autobot.io.listening.MicFrameSource` that runs
the input through Apple's Voice-Processing audio unit (via AVAudioEngine), which
performs acoustic echo cancellation (AEC) plus noise suppression. With AEC, the
barge-in monitor hears only *you* — not Jack's own voice coming back through the
speakers — so the assistant can be interrupted safely without headphones.

It exposes the same :class:`~autobot.io.listening.FrameSource` contract
(``frames()`` / ``flush()``) and yields 512-sample mono ``float32`` frames at
16 kHz, plus ``aec_active = True`` so the orchestrator knows barge-in is safe.

NOTE: this is a native CoreAudio/PyObjC path. It is imported lazily and every
failure falls back to the plain microphone (``aec_active`` then reads ``False``,
so barge-in simply won't engage). It needs validation on real macOS hardware —
the exact reference-signal routing for VPIO can vary by OS version. Requires the
optional ``aec`` extra (``uv sync --extra aec``: pyobjc AVFoundation/CoreAudio).
"""

from __future__ import annotations

import queue
import time
from collections.abc import Iterator

import numpy as np

from autobot.config import Settings
from autobot.core.types import AudioClip
from autobot.logging_setup import get_logger

_log = get_logger("listening")

_TARGET_RATE = 16_000
_FRAME = 512  # samples per emitted frame (32 ms @ 16 kHz), matching MicFrameSource


class VoiceProcessingMicSource:
    """AEC microphone via AVAudioEngine voice processing (macOS only).

    Constructing it starts the engine; raises if the platform/runtime can't provide
    voice processing, so the caller can fall back to the plain mic.
    """

    aec_active = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue[AudioClip] = queue.Queue()
        self._buf: AudioClip = np.zeros(0, dtype=np.float32)
        self._started = False
        self._warned = False  # so a per-frame tap error logs only once
        self._engine: object | None = None
        self._start_engine()

    def _start_engine(self) -> None:
        """Start an AVAudioEngine with voice processing enabled on the input node."""
        # Imported here so the module stays importable without pyobjc, and any
        # failure degrades to the plain mic rather than crashing startup.
        import objc  # noqa: F401  (ensures pyobjc is present)
        from AVFoundation import AVAudioEngine

        engine = AVAudioEngine.alloc().init()
        input_node = engine.inputNode()
        # Enable Apple's voice processing (AEC + noise suppression + AGC).
        ok, err = input_node.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"voice processing unavailable: {err}")

        in_format = input_node.outputFormatForBus_(0)
        src_rate = float(in_format.sampleRate()) or 48_000.0

        def tap(buffer: object, _when: object) -> None:
            # This runs on a realtime audio thread: an exception here is uncaught by
            # ObjC and would terminate the whole app, so we must never let it raise.
            try:
                self._queue.put(_buffer_to_mono16k(buffer, src_rate))
            except Exception:  # drop the frame, warn once, keep the app alive
                if not self._warned:
                    self._warned = True
                    _log.exception("AEC tap conversion failed; dropping frames")

        input_node.installTapOnBus_bufferSize_format_block_(0, 1024, in_format, tap)
        engine.prepare()
        ok, err = engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"audio engine failed to start: {err}")
        self._engine = engine
        # Verify the tap actually delivers audio. Some setups start the engine but
        # never pull from the input (the tap never fires) — that would block capture
        # forever. If no frame arrives quickly, fail so the caller falls back to the
        # plain mic instead of going deaf.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self._queue.empty():
            time.sleep(0.05)
        if self._queue.empty():
            raise RuntimeError(
                "voice-processing mic produced no audio within 2s — likely the process "
                "lacks Microphone permission (System Settings → Privacy & Security → "
                "Microphone) or another app holds the input"
            )
        self._started = True
        _log.info("voice-processing mic started (AEC on) src_rate=%.0f", src_rate)

    def flush(self) -> None:
        """Drop everything currently queued (audio captured while we were busy)."""
        self._buf = np.zeros(0, dtype=np.float32)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def frames(self) -> Iterator[AudioClip]:
        """Yield fixed 512-sample 16 kHz mono float32 frames forever."""
        while True:
            chunk = self._queue.get()
            self._buf = np.concatenate([self._buf, chunk])
            while self._buf.size >= _FRAME:
                yield self._buf[:_FRAME].copy()
                self._buf = self._buf[_FRAME:]


def _buffer_to_mono16k(buffer: object, src_rate: float) -> AudioClip:
    """Convert an AVAudioPCMBuffer's first channel to mono float32 at 16 kHz."""
    frames = int(buffer.frameLength())  # type: ignore[attr-defined]
    if frames <= 0:
        return np.zeros(0, dtype=np.float32)
    mono = _channel0_floats(buffer, frames)
    if abs(src_rate - _TARGET_RATE) < 1.0:
        return mono
    # Linear-interpolation resample to 16 kHz (good enough for VAD/STT framing).
    n_out = round(frames * _TARGET_RATE / src_rate)
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0, frames - 1, n_out)
    return np.asarray(np.interp(idx, np.arange(frames), mono), dtype=np.float32)


def _channel0_floats(buffer: object, frames: int) -> AudioClip:
    """Read channel 0 of an AVAudioPCMBuffer as a float32 array (PyObjC C array).

    ``floatChannelData()`` returns a C ``float **``; PyObjC exposes the inner
    ``float *`` as a varlist. ``as_buffer(nbytes)`` gives a zero-copy memoryview we
    wrap with numpy; ``as_tuple(count)`` is the slower, more-portable fallback.
    """
    ch0 = buffer.floatChannelData()[0]  # type: ignore[attr-defined]
    try:
        # as_buffer(length) exposes `length` *elements* (float32) as a memoryview;
        # copy out of it since the realtime buffer is reused after the callback.
        view = ch0.as_buffer(frames)
        return np.frombuffer(view, dtype=np.float32, count=frames).astype(np.float32).copy()
    except (AttributeError, TypeError, ValueError):
        return np.asarray(ch0.as_tuple(frames), dtype=np.float32)
