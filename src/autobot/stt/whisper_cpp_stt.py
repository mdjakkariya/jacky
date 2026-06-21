"""whisper.cpp implementation of :class:`~autobot.core.interfaces.SpeechToText`.

The GPU-accelerated alternative to the faster-whisper (CTranslate2) engine. On
Apple Silicon, whisper.cpp builds with a Metal backend, so it runs larger models
(``medium.en``, ``large-v3``) on the GPU — much faster than CTranslate2, which is
CPU-only on macOS. Same English-only contract and the same model names.

Backed by ``pywhispercpp`` (the maintained Python binding), imported lazily so
this module — and the test suite — stays importable without the optional
``whispercpp`` extra. The pure :func:`transcription_from_segments` helper is
unit-tested with fake segments; no binary, no model download.

Note: whisper.cpp's Python binding doesn't surface a per-segment log-probability,
so confidence is reported as a fixed value rather than a real model score. The
wake gate matches on the transcript text, not confidence, so this is cosmetic.
"""

from __future__ import annotations

from typing import Any

from autobot.config import Settings
from autobot.core.types import AudioClip, Transcription
from autobot.logging_setup import get_logger

_log = get_logger("stt")

# whisper.cpp gives no log-prob through the binding; report a neutral confidence
# so the transcript/logs have a value. (faster-whisper reports a real score.)
_FIXED_CONFIDENCE = 0.9


def _seg_text(segment: Any) -> str:
    """Read a segment's text whether it's an object (``.text``) or a dict."""
    if isinstance(segment, dict):
        return str(segment.get("text", ""))
    return str(getattr(segment, "text", "") or "")


def transcription_from_segments(segments: Any) -> Transcription:
    """Join whisper.cpp segments into a :class:`Transcription`.

    Empty/whitespace-only output yields a zero-confidence empty transcription, so
    callers treat "heard nothing" uniformly across engines.
    """
    parts = [t for seg in (segments or []) if (t := _seg_text(seg).strip())]
    text = " ".join(parts).strip()
    return Transcription(text=text, confidence=_FIXED_CONFIDENCE if text else 0.0)


class WhisperCppSTT:
    """Transcribes short English command clips with whisper.cpp (Metal on macOS)."""

    def __init__(self, settings: Settings) -> None:
        from pywhispercpp.model import Model

        self._settings = settings
        _log.info("loading whisper.cpp model=%s (Metal on Apple Silicon)", settings.stt_model)
        print(
            f"[stt] Loading whisper.cpp '{settings.stt_model}' (GPU/Metal)… "
            "(first run downloads the model — may take a minute)"
        )
        # Keep whisper.cpp quiet; we already log at the seams.
        self._model = Model(
            settings.stt_model,
            print_realtime=False,
            print_progress=False,
        )
        print("[stt] ready.")

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Transcribe one mono ``float32`` 16 kHz clip; see the interface contract."""
        if audio.size == 0:
            return Transcription(text="", confidence=0.0)
        # pywhispercpp accepts a 16 kHz mono float32 numpy array directly.
        segments = self._model.transcribe(audio, language="en")
        return transcription_from_segments(segments)
