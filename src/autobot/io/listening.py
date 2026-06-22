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
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from autobot.config import Settings
from autobot.core.events import AmplitudeSink
from autobot.core.types import AudioClip
from autobot.io.endpointing import (
    FramePrebuffer,
    TrailingSilenceEndpointer,
    float_to_int16,
    rms_level,
)
from autobot.io.wake_vad import VoiceActivity, WakeDetector
from autobot.logging_setup import get_logger

_log = get_logger("listening")

FRAME_SAMPLES = 512
"""Frame size fed to the models: 512 samples = 32 ms at 16 kHz (silero's window)."""

_FRAME_MS = FRAME_SAMPLES / 16_000 * 1000  # 32.0
_PREROLL_FRAMES = 10  # ~320 ms of audio kept before the wake word fires
_START_FRAMES = 2  # frames of speech needed to consider the utterance started
_MAX_WAIT_FOR_SPEECH_S = 4.0  # give up if no speech follows the wake word
# With echo cancellation the mic is already noise-suppressed and quieter, so a high
# VAD threshold starves detection (speech never crosses it). Cap it for AEC inputs.
_AEC_VAD_CEILING = 0.5


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


def capture_utterance(
    next_frame: Callable[[], AudioClip | None],
    vad: VoiceActivity,
    *,
    vad_threshold: float,
    end_silence_frames: int,
    max_frames: int,
    wait_frames: int | None,
    start_frames: int = _START_FRAMES,
    seed: Sequence[AudioClip] = (),
    on_level: AmplitudeSink | None = None,
    on_speech_start: Callable[[], None] | None = None,
    on_voice: Callable[[bool], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> AudioClip | None:
    """Capture one VAD-delimited utterance from a frame stream.

    Pulls frames from ``next_frame`` (after any ``seed`` frames), keeps a rolling
    pre-roll so the first syllable isn't clipped, and stops at the end-of-speech
    endpoint or ``max_frames``. Waits up to ``wait_frames`` for speech to start
    (``None`` = wait indefinitely). Returns the clip, or ``None`` if no speech
    started — so STT is never fed silence (Whisper hallucinates on it).

    If ``on_level`` is given, each processed frame's normalized loudness is
    reported to it (drives the orb while listening); it must not block.
    """
    endpointer = TrailingSilenceEndpointer(
        speech_threshold=vad_threshold,
        start_frames=start_frames,
        end_silence_frames=end_silence_frames,
    )
    prebuffer = FramePrebuffer(_PREROLL_FRAMES)
    collected: list[AudioClip] | None = None
    waited = 0
    voiced = False  # whether we've signalled "speech active" (so we reset it after)

    def stream() -> Iterator[AudioClip]:
        yield from seed
        while True:
            frame = next_frame()
            if frame is None:
                return
            yield frame

    try:
        for frame in stream():
            if on_level is not None:
                on_level(rms_level(frame))
            endpointer.update(vad.speech_prob(frame))
            if collected is None:
                # Give up waiting if a caller-supplied predicate says to stop (used
                # for barge-in monitoring: stop once playback has finished).
                if should_continue is not None and not should_continue():
                    return None
                # Still waiting for speech to begin: keep a rolling pre-roll.
                prebuffer.push(frame)
                if endpointer.started:
                    collected = prebuffer.drain()  # include the pre-roll lead-in
                    if on_speech_start is not None:
                        on_speech_start()  # stamp when the user actually began speaking
                    if on_voice is not None:
                        on_voice(True)  # the user is speaking -> orb shows "listening"
                        voiced = True
                else:
                    waited += 1
                    if wait_frames is not None and waited >= wait_frames:
                        return None
            else:
                collected.append(frame)
                if endpointer.finished or len(collected) >= max_frames:
                    break

        if collected is None or not endpointer.started:
            return None
        return np.concatenate(collected).astype(np.float32)
    finally:
        # Always drop the "listening" cue when capture ends (speech finished, hit the
        # cap, or stream closed), so the orb doesn't stay stuck animating.
        if voiced and on_voice is not None:
            on_voice(False)


class WakeWordVadRecorder:
    """Captures one utterance per call, triggered by a wake word and ended by VAD."""

    def __init__(
        self,
        settings: Settings,
        source: FrameSource,
        wake: WakeDetector,
        vad: VoiceActivity,
        on_level: AmplitudeSink | None = None,
        reload: Callable[[], Settings] | None = None,
        on_voice: Callable[[bool], None] | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._wake = wake
        self._vad = vad
        self._on_level = on_level
        self._on_voice = on_voice
        # Only animate "listening" while engaged in a conversation; the orchestrator
        # sets this so passive (non-addressed) capture doesn't light up the orb.
        self._awake = False
        self._reload = reload
        self._frames: Iterator[AudioClip] | None = None
        self._max_wait_frames = max(1, round(_MAX_WAIT_FOR_SPEECH_S * 1000 / _FRAME_MS))
        self._refresh_tunables()
        # After a turn we stay "open" for a follow-up without the wake word.
        self._follow_up_active = False
        # Monotonic time the most recent capture's speech began (for follow-up timing).
        self._last_speech_started_at: float | None = None

    @property
    def last_speech_started_at(self) -> float | None:
        """When the most recently captured speech began (``time.monotonic``)."""
        return self._last_speech_started_at

    def _refresh_tunables(self) -> None:
        """Recompute frame thresholds from settings; re-read live if a reloader is set.

        Lets the Settings view tune the end-of-speech pause and max length without a
        restart. Tests inject no reloader, so they keep their fixed settings.
        """
        if self._reload is not None:
            self._settings = self._reload()
        s = self._settings
        self._end_silence_frames = max(1, round(s.end_silence_ms / _FRAME_MS))
        self._max_frames = max(1, round(s.max_utterance_s * 1000 / _FRAME_MS))
        self._follow_up_frames = max(0, round(s.follow_up_window_s * 1000 / _FRAME_MS))
        self._wake_preroll_frames = max(0, round(s.wake_preroll_ms / _FRAME_MS))
        # Barge-in needs *sustained* speech (not a 2-frame flicker) so residual echo
        # or a transient never falsely interrupts Jack mid-reply.
        self._barge_in_start_frames = max(_START_FRAMES, round(s.barge_in_min_speech_ms / _FRAME_MS))

    def _next_frame(self) -> AudioClip | None:
        """Pull the next frame from the (persistent) source, or ``None`` if ended."""
        if self._frames is None:
            self._frames = iter(self._source.frames())
        return next(self._frames, None)

    def _mark_speech_start(self) -> None:
        self._last_speech_started_at = time.monotonic()

    def set_awake(self, awake: bool) -> None:
        """Whether to surface the 'listening' cue (engaged) for the next capture."""
        self._awake = awake

    def _voice(self, active: bool) -> None:
        if self._awake and self._on_voice is not None:
            self._on_voice(active)

    def _vad_threshold(self) -> float:
        """Speech threshold, capped for AEC mics (else a high value never triggers)."""
        t = self._settings.vad_threshold
        if t > _AEC_VAD_CEILING and bool(getattr(self._source, "aec_active", False)):
            return _AEC_VAD_CEILING
        return t

    def flush(self) -> None:
        """Drop buffered audio and reset VAD — so a fresh capture starts clean."""
        self._source.flush()
        self._vad.reset()

    def _capture(self, wait_frames: int, seed: Sequence[AudioClip] = ()) -> AudioClip | None:
        """Capture one utterance via the shared helper (see :func:`capture_utterance`)."""
        return capture_utterance(
            self._next_frame,
            self._vad,
            vad_threshold=self._vad_threshold(),
            end_silence_frames=self._end_silence_frames,
            max_frames=self._max_frames,
            wait_frames=wait_frames,
            seed=seed,
            on_level=self._on_level,
            on_speech_start=self._mark_speech_start,
            on_voice=self._voice,
        )

    def record_clip(self) -> AudioClip:
        """Capture one turn: a follow-up if still in conversation, else wake-word first.

        See the module docstring for the full flow.
        """
        # Pick up any Settings-view changes to the endpointing tunables (no restart).
        self._refresh_tunables()
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

        # Idle until the wake word fires, keeping a short rolling pre-roll so a
        # command spoken in the same breath as the wake word isn't clipped.
        print("[mic] Listening for the wake word…")
        wake_preroll = FramePrebuffer(self._wake_preroll_frames or 1)
        threshold = self._settings.wake_threshold
        while True:
            frame = self._next_frame()
            if frame is None:
                return np.zeros(0, dtype=np.float32)
            score = self._wake.score(float_to_int16(frame))
            # Log near-misses so the threshold can be tuned from real data — a
            # continuous wake word peaks lower than an isolated one.
            if score >= 0.2:
                _log.debug("wake score=%.2f threshold=%.2f", score, threshold)
            if score >= threshold:
                _log.info("wake fired score=%.2f threshold=%.2f", score, threshold)
                break
            wake_preroll.push(frame)

        seed = wake_preroll.drain() if self._wake_preroll_frames > 0 else []
        _log.info("wake detected model=%s preroll_frames=%d", self._settings.wake_model, len(seed))
        print("[mic] Wake word detected — listening for your command…")
        clip = self._capture(self._max_wait_frames, seed=seed)
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

    def record_continuation(self, max_wait_s: float = 2.0) -> AudioClip:
        """Capture a brief continuation if the user resumes speaking soon (else empty).

        Used to recover a cut-off phrase: waits ``max_wait_s`` for resumed speech
        (no flush, so audio from while we transcribed isn't lost) and returns it,
        or an empty clip if the user was actually done.
        """
        self._vad.reset()
        wait_frames = max(1, round(max_wait_s * 1000 / _FRAME_MS))
        clip = self._capture(wait_frames)
        return clip if clip is not None else np.zeros(0, dtype=np.float32)

    @property
    def aec_active(self) -> bool:
        """Whether the mic input is echo-cancelled (needed for safe barge-in)."""
        return bool(getattr(self._source, "aec_active", False))

    def monitor_barge_in(
        self,
        should_continue: Callable[[], bool],
        on_speech_start: Callable[[], None] | None = None,
    ) -> AudioClip | None:
        """Watch for a barge-in while ``should_continue()`` holds.

        Fires ``on_speech_start`` the instant speech begins (to stop playback at
        once); returns the captured utterance, or ``None`` if playback finished.
        """
        self._vad.reset()

        def started() -> None:
            self._mark_speech_start()
            if on_speech_start is not None:
                on_speech_start()

        return capture_utterance(
            self._next_frame,
            self._vad,
            vad_threshold=self._vad_threshold(),
            end_silence_frames=self._end_silence_frames,
            max_frames=self._max_frames,
            wait_frames=None,
            start_frames=self._barge_in_start_frames,
            on_level=self._on_level,
            on_speech_start=started,
            on_voice=self._voice,
            should_continue=should_continue,
        )


class VadRecorder:
    """Captures each spoken utterance with VAD only — no wake word.

    Used by the transcribe-then-match detector: every phrase is captured and
    handed to STT, and the wake word is matched on the resulting *text* (so it
    works even when spoken continuously). Implements the same ``AudioSource``
    contract, so the orchestrator is unchanged.
    """

    def __init__(
        self,
        settings: Settings,
        source: FrameSource,
        vad: VoiceActivity,
        on_level: AmplitudeSink | None = None,
        reload: Callable[[], Settings] | None = None,
        on_voice: Callable[[bool], None] | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._vad = vad
        self._on_level = on_level
        self._on_voice = on_voice
        # Only animate "listening" while engaged in a conversation; the orchestrator
        # sets this so passive (non-addressed) capture doesn't light up the orb.
        self._awake = False
        self._reload = reload
        self._frames: Iterator[AudioClip] | None = None
        self._last_speech_started_at: float | None = None
        self._refresh_tunables()

    @property
    def last_speech_started_at(self) -> float | None:
        """When the most recently captured speech began (``time.monotonic``)."""
        return self._last_speech_started_at

    def _refresh_tunables(self) -> None:
        """Recompute frame thresholds from settings; re-read live if a reloader is set."""
        if self._reload is not None:
            self._settings = self._reload()
        s = self._settings
        self._end_silence_frames = max(1, round(s.end_silence_ms / _FRAME_MS))
        self._max_frames = max(1, round(s.max_utterance_s * 1000 / _FRAME_MS))
        # Barge-in needs *sustained* speech so residual echo can't falsely interrupt.
        self._barge_in_start_frames = max(_START_FRAMES, round(s.barge_in_min_speech_ms / _FRAME_MS))

    def _mark_speech_start(self) -> None:
        self._last_speech_started_at = time.monotonic()

    def set_awake(self, awake: bool) -> None:
        """Whether to surface the 'listening' cue (engaged) for the next capture."""
        self._awake = awake

    def _voice(self, active: bool) -> None:
        if self._awake and self._on_voice is not None:
            self._on_voice(active)

    def _vad_threshold(self) -> float:
        """Speech threshold, capped for AEC mics (else a high value never triggers)."""
        t = self._settings.vad_threshold
        if t > _AEC_VAD_CEILING and bool(getattr(self._source, "aec_active", False)):
            return _AEC_VAD_CEILING
        return t

    def flush(self) -> None:
        """Drop buffered audio and reset VAD — so a fresh capture starts clean.

        Used before listening for a confirmation answer, so audio from before the
        question (a command tail, speech during planning) can't be misread as a yes.
        """
        self._source.flush()
        self._vad.reset()

    def _next_frame(self) -> AudioClip | None:
        if self._frames is None:
            self._frames = iter(self._source.frames())
        return next(self._frames, None)

    def record_clip(self) -> AudioClip:
        """Wait for the next spoken phrase and return it (VAD-delimited)."""
        self._refresh_tunables()  # pick up Settings-view changes (no restart)
        self._source.flush()
        self._vad.reset()
        print("[mic] Listening…")
        clip = capture_utterance(
            self._next_frame,
            self._vad,
            vad_threshold=self._vad_threshold(),
            end_silence_frames=self._end_silence_frames,
            max_frames=self._max_frames,
            wait_frames=None,  # wait indefinitely for speech
            on_level=self._on_level,
            on_speech_start=self._mark_speech_start,
            on_voice=self._voice,
        )
        if clip is None:
            return np.zeros(0, dtype=np.float32)
        seconds = clip.size / self._settings.sample_rate
        _log.info("captured seconds=%.1f", seconds)
        return clip

    def record_continuation(self, max_wait_s: float = 2.0) -> AudioClip:
        """Capture a brief continuation if the user resumes speaking soon (else empty).

        Recovers a cut-off phrase: waits ``max_wait_s`` for resumed speech (no flush,
        so audio captured while we transcribed isn't lost) and returns it, or an
        empty clip if the user was actually finished.
        """
        self._vad.reset()
        wait_frames = max(1, round(max_wait_s * 1000 / _FRAME_MS))
        clip = capture_utterance(
            self._next_frame,
            self._vad,
            vad_threshold=self._vad_threshold(),
            end_silence_frames=self._end_silence_frames,
            max_frames=self._max_frames,
            wait_frames=wait_frames,
            on_level=self._on_level,
            on_voice=self._voice,
        )
        return clip if clip is not None else np.zeros(0, dtype=np.float32)

    @property
    def aec_active(self) -> bool:
        """Whether the mic input is echo-cancelled (needed for safe barge-in)."""
        return bool(getattr(self._source, "aec_active", False))

    def monitor_barge_in(
        self,
        should_continue: Callable[[], bool],
        on_speech_start: Callable[[], None] | None = None,
    ) -> AudioClip | None:
        """While ``should_continue()`` holds, watch for the user starting to speak.

        ``on_speech_start`` fires the *instant* speech begins (used to stop playback
        immediately, before the whole utterance is captured). Returns the captured
        utterance the moment they barge in (through to its end), or ``None`` if
        playback finished first.
        """
        self._vad.reset()

        def started() -> None:
            self._mark_speech_start()
            if on_speech_start is not None:
                on_speech_start()

        return capture_utterance(
            self._next_frame,
            self._vad,
            vad_threshold=self._vad_threshold(),
            end_silence_frames=self._end_silence_frames,
            max_frames=self._max_frames,
            wait_frames=None,
            start_frames=self._barge_in_start_frames,
            on_level=self._on_level,
            on_speech_start=started,
            on_voice=self._voice,
            should_continue=should_continue,
        )
