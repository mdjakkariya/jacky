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
                _log.info("building voice I/O on first use (mic + tts)")
                self._io = self._factory()
            return self._io

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

    def record_continuation(self, timeout_s: float) -> Any:
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
