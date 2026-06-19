"""faster-whisper implementation of :class:`~autobot.core.interfaces.SpeechToText`.

English-only by construction: the model is an ``*.en`` build and decoding is
pinned to English with no language autodetect. The heavy ``faster_whisper``
import happens lazily in :meth:`__init__` so importing this module is cheap.

Swapping to another engine (Moonshine, Parakeet) means writing a sibling class
that satisfies the same protocol — nothing downstream changes.
"""

from __future__ import annotations

import math

from autobot.config import Settings
from autobot.core.types import AudioClip, Transcription


class FasterWhisperSTT:
    """Transcribes short English command clips with faster-whisper."""

    def __init__(self, settings: Settings) -> None:
        from faster_whisper import WhisperModel

        self._settings = settings
        print(
            f"[stt] Loading faster-whisper '{settings.stt_model}' "
            f"({settings.stt_device}/{settings.stt_compute_type})…"
        )
        self._model = WhisperModel(
            settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
        )

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Transcribe one mono ``float32`` clip; see the interface for the contract."""
        if audio.size == 0:
            return Transcription(text="", confidence=0.0)

        segments, _info = self._model.transcribe(
            audio,
            language="en",  # English-only: never autodetect
            beam_size=1,  # commands are short; greedy is fast and fine
            vad_filter=False,  # real VAD arrives in Phase 2
        )

        texts: list[str] = []
        logprobs: list[float] = []
        for segment in segments:
            texts.append(segment.text.strip())
            logprobs.append(segment.avg_logprob)

        text = " ".join(t for t in texts if t).strip()
        # Convert mean log-probability into a rough 0..1 confidence.
        confidence = math.exp(sum(logprobs) / len(logprobs)) if logprobs else 0.0
        return Transcription(text=text, confidence=confidence)
