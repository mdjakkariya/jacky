"""Lazily build the voice I/O (mic + TTS + recorder) on first real use.

Chat-first reliability: a fresh, chat-default launch must not open the microphone
(no permission prompt) or load the STT/voice models — those are only needed once the
user actually switches to voice. The voice loop is the *only* caller of these objects
and it runs solely in voice mode, so the first ``record_clip()`` / ``speak()`` (or a
voice confirmation) triggers a single, lazy build. Building the mic, TTS and recorder
*together* preserves the AEC routing (TTS played through the mic engine for barge-in).

Reading a plain attribute (e.g. ``aec_active``) never forces a build — it reports as
absent until the real I/O exists, so startup checks stay cheap.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from autobot.core.interfaces import AudioSource, TextToSpeech
from autobot.logging_setup import get_logger

_log = get_logger("listening")

VoiceIOFactory = Callable[[], tuple[AudioSource, TextToSpeech]]


class LazyVoiceIO:
    """Builds ``(audio, tts)`` once, on first use, behind thin proxies."""

    def __init__(self, factory: VoiceIOFactory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._io: tuple[AudioSource, TextToSpeech] | None = None

    def _ensure(self) -> tuple[AudioSource, TextToSpeech]:
        with self._lock:
            if self._io is None:
                _log.info("building voice I/O (mic + tts)")
                self._io = self._factory()
            return self._io

    def release(self) -> None:
        """Tear down the built voice I/O so the mic is released (idempotent).

        Closing the audio source stops the OS audio engine — on macOS that's what
        stops the Voice-Processing unit from ducking all other system audio while
        Jack sits in chat mode. The next ``audio``/``tts`` use rebuilds everything
        fresh (a fresh engine is more reliable than restarting the duplex unit in
        place). Safe to call when nothing is built yet.
        """
        with self._lock:
            if self._io is None:
                return
            audio, _tts = self._io
            self._io = None
        # Close outside the lock: closing stops the engine and unblocks any capture
        # parked in the source, which must not deadlock against a concurrent _ensure.
        close = getattr(audio, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # never let teardown raise into the caller
                _log.exception("voice I/O close failed")
        _log.info("voice I/O released (mic + tts torn down)")

    @property
    def audio(self) -> AudioSource:
        """A proxy AudioSource; builds the real mic/recorder on first method call."""
        return _LazyAudio(self)

    @property
    def tts(self) -> TextToSpeech:
        """A proxy TextToSpeech; builds the real engine on first ``speak``/``stop``."""
        return _LazyTTS(self)


class _LazyAudio:
    """Delegates to the real AudioSource, building it the first time a method runs."""

    def __init__(self, holder: LazyVoiceIO) -> None:
        self._holder = holder

    def _audio(self) -> AudioSource:
        return self._holder._ensure()[0]

    def record_clip(self) -> Any:
        return self._audio().record_clip()

    def record_continuation(self, timeout_s: float = 2.0) -> Any:
        # Keep the real recorder's default (``max_wait_s=2.0``) so callers that rely
        # on it — e.g. the orchestrator's cut-off re-open, which calls this with no
        # argument — work through the proxy instead of raising a missing-arg error.
        fn = getattr(self._audio(), "record_continuation", None)
        return fn(timeout_s) if callable(fn) else None

    def monitor_barge_in(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._audio(), "monitor_barge_in", None)
        return fn(*args, **kwargs) if callable(fn) else None

    def set_awake(self, awake: bool) -> None:
        fn = getattr(self._audio(), "set_awake", None)
        if callable(fn):
            fn(awake)

    def flush(self) -> None:
        fn = getattr(self._audio(), "flush", None)
        if callable(fn):
            fn()

    def __getattr__(self, name: str) -> Any:
        # Only value-attributes (e.g. ``last_speech_started_at``, ``aec_active``) reach
        # here. Don't build just to read one: report absent until the real I/O exists,
        # so ``getattr(audio, name, default)`` at startup stays cheap and mic-free.
        holder = self.__dict__["_holder"]
        if holder._io is None:
            raise AttributeError(name)
        return getattr(holder._io[0], name)


class _LazyTTS:
    """Delegates to the real TextToSpeech, building it on first ``speak``/``stop``."""

    def __init__(self, holder: LazyVoiceIO) -> None:
        self._holder = holder

    def speak(self, text: str) -> None:
        self._holder._ensure()[1].speak(text)

    def stop(self) -> None:
        # Only stop if the engine actually exists — nothing to stop before first use.
        if self._holder._io is not None:
            self._holder._io[1].stop()
