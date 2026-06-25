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

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import numpy as np

from autobot.config import Settings
from autobot.core.events import AmplitudeSink
from autobot.core.types import AudioClip, Int16Frame
from autobot.logging_setup import get_logger

_log = get_logger("listening")

_TARGET_RATE = 16_000
_FRAME = 512  # samples per emitted frame (32 ms @ 16 kHz), matching MicFrameSource


class VoiceProcessingMicSource:
    """AEC microphone via AVAudioEngine voice processing (macOS only).

    Constructing it starts the engine; raises if the platform/runtime can't provide
    voice processing, so the caller can fall back to the plain mic.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue[AudioClip] = queue.Queue()
        self._buf: AudioClip = np.zeros(0, dtype=np.float32)
        self._started = False
        # Set by close() to release the mic when leaving voice mode (so the
        # Voice-Processing unit stops ducking other system audio). frames() polls it.
        self._stopped = threading.Event()
        self._warned = False  # so a per-frame tap error logs only once
        self._engine: Any = None
        self._player_node: Any = None
        self._out_format: Any = None
        self._out_rate = float(_TARGET_RATE)
        self._src_rate = 48_000.0
        self._play_lock = threading.Lock()
        # aec_active means the *full-duplex* path is live: TTS renders through this
        # engine so its voice is cancelled and barge-in is safe. It's only set True
        # when playback wiring succeeds; capture-only fallback leaves it False so the
        # orchestrator runs half-duplex (and Jack can't hear itself).
        self.aec_active = False
        self._start_engine()

    def _start_engine(self) -> None:
        """Bring up the AEC engine, preferring full-duplex but never breaking.

        Voice-processing playback is device-finicky — on some mic/speaker combos the
        duplex unit refuses to initialize (CoreAudio -10875, "input and output
        formats do not match"). So we try full-duplex first (TTS rendered through the
        engine as the echo reference, enabling safe barge-in); if that won't start, we
        fall back to capture-only AEC and half-duplex; if even that fails, we raise so
        the caller uses the plain mic. Either way the app keeps working.
        """
        import objc  # noqa: F401  (ensures pyobjc is present)

        try:
            self._build(with_playback=True)
            self.aec_active = True  # full-duplex: route TTS here, barge-in safe
            _log.info("voice-processing full-duplex (AEC + playback) rate=%.0f", self._out_rate)
            return
        except Exception as exc:
            _log.warning("AEC playback unavailable (%s) — capture-only, half-duplex", exc)
            self._player_node = None

        # Capture-only fallback: mic is still echo-cancelled, but TTS plays through the
        # normal output, so we stay half-duplex (aec_active=False) to avoid self-echo.
        self._build(with_playback=False)
        _log.info("voice-processing capture-only (half-duplex) rate=%.0f", self._src_rate)

    def _build(self, with_playback: bool) -> None:
        """Construct + start the engine, optionally wiring the TTS playback node."""
        from AVFoundation import AVAudioEngine, AVAudioPlayerNode

        # Tear down any prior attempt and start from clean buffers.
        if self._engine is not None:
            with contextlib.suppress(Exception):
                self._engine.stop()
        self._engine = None
        self._player_node = None
        self._queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.float32)

        engine = AVAudioEngine.alloc().init()
        input_node = engine.inputNode()
        ok, err = input_node.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"voice processing unavailable: {err}")
        _minimize_other_audio_ducking(input_node)
        in_format = input_node.outputFormatForBus_(0)
        self._src_rate = float(in_format.sampleRate()) or 48_000.0
        self._out_rate = self._src_rate

        if with_playback:
            # Match the output node's own format (channels + rate) so the duplex unit
            # initializes; a mismatched custom format triggers -10875.
            out_node = engine.outputNode()
            out_format = out_node.inputFormatForBus_(0)
            player = AVAudioPlayerNode.alloc().init()
            engine.attachNode_(player)
            engine.connect_to_format_(player, out_node, out_format)
            self._player_node = player
            self._out_format = out_format
            self._out_rate = float(out_format.sampleRate()) or self._src_rate

        src_rate = self._src_rate

        def tap(buffer: object, _when: object) -> None:
            # Realtime audio thread: an uncaught ObjC exception would kill the app.
            try:
                self._queue.put(_buffer_to_mono16k(buffer, src_rate))
            except Exception:
                if not self._warned:
                    self._warned = True
                    _log.exception("AEC tap conversion failed; dropping frames")

        input_node.installTapOnBus_bufferSize_format_block_(0, 1024, in_format, tap)
        engine.prepare()
        ok, err = engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"audio engine failed to start: {err}")
        self._engine = engine
        # Verify the tap delivers audio quickly, else we'd block capture forever.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self._queue.empty():
            time.sleep(0.05)
        if self._queue.empty():
            raise RuntimeError(
                "voice-processing mic produced no audio within 2s — likely the process "
                "lacks Microphone permission (System Settings → Privacy & Security → "
                "Microphone) or another app holds the input"
            )
        if with_playback and self._player_node is not None:
            self._player_node.play()  # idle until buffers are scheduled by play()
        self._started = True

    def flush(self) -> None:
        """Drop everything currently queued (audio captured while we were busy)."""
        self._buf = np.zeros(0, dtype=np.float32)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def frames(self) -> Iterator[AudioClip]:
        """Yield fixed 512-sample 16 kHz mono float32 frames until closed.

        The blocking read uses a short timeout so :meth:`close` can interrupt a
        capture that's parked waiting for audio (e.g. on the wake word) — otherwise
        leaving voice mode couldn't release the mic until the user happened to speak.
        """
        while not self._stopped.is_set():
            try:
                chunk = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._buf = np.concatenate([self._buf, chunk])
            while self._buf.size >= _FRAME:
                yield self._buf[:_FRAME].copy()
                self._buf = self._buf[_FRAME:]

    def close(self) -> None:
        """Stop the engine and release the microphone (idempotent).

        Tearing the Voice-Processing unit down is what stops macOS from ducking all
        other audio while Jack is in chat mode. A fresh engine is built on the next
        switch back to voice (the proven first-use path), so we don't try to restart
        this finicky duplex unit in place.
        """
        self._stopped.set()
        engine, self._engine = self._engine, None
        node, self._player_node = self._player_node, None
        self._started = False
        if node is not None:
            with contextlib.suppress(Exception):
                node.stop()
        if engine is not None:
            with contextlib.suppress(Exception):
                engine.stop()
        _log.info("voice-processing mic closed (released)")

    def play(
        self,
        audio: Int16Frame,
        sample_rate: int,
        cancel: threading.Event,
        on_level: AmplitudeSink | None = None,
    ) -> bool:
        """Play int16 PCM through the voice-processing output (the AEC reference).

        Implements the TTS :class:`~autobot.tts.piper_tts.AudioPlayer` protocol so
        Jack's speech is rendered through the same engine that captures the mic —
        which is what lets macOS cancel his voice and makes barge-in safe on speakers.
        Returns True if it played to completion, False if ``cancel`` interrupted it.
        Any native failure is swallowed (logged) and reported as completed, so a
        playback glitch never crashes a turn.
        """
        node, fmt = self._player_node, self._out_format
        if node is None or fmt is None or audio.size == 0:
            return True
        try:
            from AVFoundation import AVAudioPCMBuffer

            # int16 -> float32 [-1, 1], resampled to the engine's rate.
            samples = audio.astype(np.float32) / 32768.0
            if abs(sample_rate - self._out_rate) >= 1.0:
                n_out = max(1, round(samples.size * self._out_rate / sample_rate))
                samples = np.asarray(
                    np.interp(
                        np.linspace(0, samples.size - 1, n_out),
                        np.arange(samples.size),
                        samples,
                    ),
                    dtype=np.float32,
                )
            n = samples.size
            channels = max(1, int(fmt.channelCount()))
            with self._play_lock:
                buf = AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, n)
                if buf is None:
                    return True
                buf.setFrameLength_(n)
                # Write the mono signal into every channel of the output format.
                channel_data = buf.floatChannelData()
                for c in range(channels):
                    view = channel_data[c].as_buffer(n)  # writable memoryview, n float32
                    np.frombuffer(view, dtype=np.float32, count=n)[:] = samples
                node.scheduleBuffer_completionHandler_(buf, None)
                if not node.isPlaying():
                    node.play()
            # Poll to completion, honoring cancel and driving the orb level.
            block = max(1, int(self._out_rate / 30))
            idx = 0
            while idx < n:
                if cancel.is_set():
                    node.stop()
                    node.play()  # leave the node ready for the next reply
                    if on_level is not None:
                        on_level(0.0)
                    return False
                if on_level is not None:
                    chunk = samples[idx : idx + block]
                    on_level(float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0)
                time.sleep(block / self._out_rate)
                idx += block
            if on_level is not None:
                on_level(0.0)
            return True
        except Exception:  # never let a playback glitch take down the turn
            _log.exception("AEC playback failed; reply may be inaudible this turn")
            if on_level is not None:
                on_level(0.0)
            return True


def _minimize_other_audio_ducking(input_node: Any) -> None:
    """Stop voice processing from ducking *other* system audio to near-silence.

    By default Apple's Voice-Processing unit treats every audio stream that isn't our
    captured voice as "other audio" and ducks it hard so speech stays intelligible —
    which makes any video/music play almost inaudibly the whole time the mic engine is
    open. macOS 14 added ``voiceProcessingOtherAudioDuckingConfiguration`` to control
    this; we set it to the minimum level with advanced ducking off, so other audio
    keeps playing at normal volume while Jack listens.

    Best-effort and fully guarded: on older macOS / runtimes without the selector this
    is a no-op (AEC still works, just with the OS default ducking) so it can never
    break capture. The duck level constant ``10`` is
    ``AVAudioVoiceProcessingOtherAudioDuckingLevelMin``.
    """
    setter = getattr(input_node, "setVoiceProcessingOtherAudioDuckingConfiguration_", None)
    if setter is None:  # pre-macOS 14 or unavailable in this runtime — leave OS default
        return
    with contextlib.suppress(Exception):
        # PyObjC marshals the C struct as a tuple: (enableAdvancedDucking, duckingLevel).
        setter((False, 10))
        _log.info("voice-processing other-audio ducking minimized")


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
