"""Piper text-to-speech: fast, on-device English synthesis (CPU).

The heavy ``piper`` runtime and ``sounddevice`` are imported lazily so importing
this module stays cheap. The voice model (an ``.onnx`` file plus its ``.json``
config) is downloaded once by the user and pointed to via ``settings.tts_voice``
(see the README). Swapping engines later (e.g. Kokoro) means writing a sibling
class with the same :meth:`speak` method — nothing else changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from autobot.config import Settings
from autobot.core.events import AmplitudeSink
from autobot.core.types import Int16Frame
from autobot.io.endpointing import rms_level
from autobot.logging_setup import get_logger

_log = get_logger("tts")


class PiperTTS:
    """Speaks replies with a Piper voice, playing audio through the speakers."""

    def __init__(self, settings: Settings, on_level: AmplitudeSink | None = None) -> None:
        from piper import PiperVoice

        voice_path = Path(settings.tts_voice).expanduser()
        if not voice_path.exists():
            raise FileNotFoundError(f"Piper voice model not found: {voice_path}")
        _log.info("loading piper voice=%s", voice_path)
        self._voice = PiperVoice.load(str(voice_path))
        self._on_level = on_level

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
        if self._on_level is None:
            sd.play(audio, sample_rate)
            sd.wait()
            return
        # Stream in ~33 ms blocks so we can report a live loudness envelope to the
        # orb (the "talking" reactive motion). ``write`` blocks until each block is
        # consumed, which paces the envelope to real playback time.
        self._play_with_levels(sd, audio, sample_rate)

    def _play_with_levels(self, sd: Any, audio: Int16Frame, sample_rate: int) -> None:
        """Play ``audio`` block-by-block, reporting each block's loudness."""
        assert self._on_level is not None
        block = max(1, sample_rate // 30)
        stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
        stream.start()
        try:
            for start in range(0, audio.size, block):
                chunk = audio[start : start + block]
                # int16 → float in [-1, 1] for the shared loudness helper.
                self._on_level(rms_level(chunk.astype(np.float32) / 32768.0))
                stream.write(chunk)
        finally:
            self._on_level(0.0)  # settle the orb back to quiet when speech ends
            stream.stop()
            stream.close()
