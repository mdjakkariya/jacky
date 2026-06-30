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
from autobot.core.types import AudioClip, Segment, Transcription
from autobot.logging_setup import get_logger

_log = get_logger("stt")

STTFactory = Callable[[], SpeechToText]


class ReloadableSTT:
    """Wraps a built :class:`SpeechToText`, rebuilding it on demand."""

    def __init__(self, factory: STTFactory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._dirty = False
        # Built LAZILY on the first transcription — not at startup. Loading (and, on a
        # fresh machine, downloading) the model is slow and only needed in voice mode,
        # so a chat-first launch must not pay that cost or block the daemon coming up.
        self._inner: SpeechToText | None = None

    def mark_dirty(self) -> None:
        """Request a model reload before the next transcription (settings changed)."""
        with self._lock:
            self._dirty = True
        _log.info("stt marked for reload")

    def _ensure(self) -> SpeechToText:
        """Build/reload the model on first use or after a settings change."""
        with self._lock:
            if self._inner is None or self._dirty:
                first = self._inner is None
                try:
                    self._inner = self._factory()
                    _log.info("stt model loaded" if first else "stt reloaded from updated settings")
                except Exception as exc:
                    if self._inner is None:
                        raise
                    _log.warning("stt reload failed, keeping current: %s", exc)
                self._dirty = False
            return self._inner

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Build/reload the model on first use or after a settings change, then transcribe."""
        return self._ensure().transcribe(audio)

    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        """Long-form transcription; same reload semantics as :meth:`transcribe`."""
        return self._ensure().transcribe_segments(
            audio,
            language=language,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt,
        )
