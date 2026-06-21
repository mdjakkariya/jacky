"""A speech-to-text proxy that can rebuild itself when settings change.

Mirrors :class:`~autobot.llm.reloadable.ReloadableLanguageModel`: the engine
builds STT once at startup, but the Settings view can change the model **without
a restart**. On the first transcription after a change, it reloads from fresh
settings (loading the newly chosen model — a one-time cost); if that load fails
it keeps the working model so a bad model name can't break transcription.

Thread-safe: ``mark_dirty`` is called from the daemon thread; ``transcribe`` runs
on the engine thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from autobot.core.interfaces import SpeechToText
from autobot.core.types import AudioClip, Transcription
from autobot.logging_setup import get_logger

_log = get_logger("stt")

STTFactory = Callable[[], SpeechToText]


class ReloadableSTT:
    """Wraps a built :class:`SpeechToText`, rebuilding it on demand."""

    def __init__(self, factory: STTFactory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._dirty = False
        self._inner: SpeechToText = factory()  # build eagerly (load model at startup)

    def mark_dirty(self) -> None:
        """Request a model reload before the next transcription (settings changed)."""
        with self._lock:
            self._dirty = True
        _log.info("stt marked for reload")

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Reload the model from fresh settings if dirty, then transcribe."""
        with self._lock:
            if self._dirty:
                try:
                    self._inner = self._factory()
                    _log.info("stt reloaded from updated settings")
                except Exception as exc:  # keep the working model on failure
                    _log.warning("stt reload failed, keeping current: %s", exc)
                self._dirty = False
            inner = self._inner
        return inner.transcribe(audio)
