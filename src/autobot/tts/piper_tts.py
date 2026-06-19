"""Piper text-to-speech: fast, on-device English synthesis (CPU).

The heavy ``piper`` runtime and ``sounddevice`` are imported lazily so importing
this module stays cheap. The voice model (an ``.onnx`` file plus its ``.json``
config) is downloaded once by the user and pointed to via ``settings.tts_voice``
(see the README). Swapping engines later (e.g. Kokoro) means writing a sibling
class with the same :meth:`speak` method — nothing else changes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from autobot.config import Settings
from autobot.logging_setup import get_logger

_log = get_logger("tts")


class PiperTTS:
    """Speaks replies with a Piper voice, playing audio through the speakers."""

    def __init__(self, settings: Settings) -> None:
        from piper import PiperVoice

        voice_path = Path(settings.tts_voice).expanduser()
        if not voice_path.exists():
            raise FileNotFoundError(f"Piper voice model not found: {voice_path}")
        _log.info("loading piper voice=%s", voice_path)
        self._voice = PiperVoice.load(str(voice_path))

    def speak(self, text: str) -> None:
        """Synthesize ``text`` and play it, blocking until playback finishes."""
        if not text.strip():
            return
        import sounddevice as sd

        # piper>=1.2: synthesize() yields one AudioChunk per sentence, each with
        # an int16 numpy array and its sample rate.
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return
        audio = np.concatenate([chunk.audio_int16_array for chunk in chunks])
        sample_rate = int(chunks[0].sample_rate)
        _log.debug("speaking chars=%d samples=%d rate=%d", len(text), audio.size, sample_rate)
        sd.play(audio, sample_rate)
        sd.wait()
