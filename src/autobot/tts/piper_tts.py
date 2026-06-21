"""Piper text-to-speech: fast, on-device English synthesis (CPU).

The heavy ``piper`` runtime and ``sounddevice`` are imported lazily so importing
this module stays cheap. The voice model (an ``.onnx`` file plus its ``.json``
config) is downloaded once by the user and pointed to via ``settings.tts_voice``
(see the README). Swapping engines later (e.g. Kokoro) means writing a sibling
class with the same :meth:`speak` method — nothing else changes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from autobot.config import Settings
from autobot.core.events import AmplitudeSink
from autobot.core.types import Int16Frame
from autobot.io.endpointing import rms_level
from autobot.logging_setup import get_logger

_log = get_logger("tts")


def play_cancellable(
    sd: Any,
    audio: Int16Frame,
    sample_rate: int,
    cancel: threading.Event,
    on_level: AmplitudeSink | None = None,
) -> bool:
    """Play int16 PCM block-by-block, stopping early if ``cancel`` is set.

    Streaming in ~33 ms blocks (and checking ``cancel`` before each) is what makes
    speech interruptible for barge-in: when the user talks over Jack, the coordinator
    sets ``cancel`` and playback aborts within a block instead of finishing the reply.
    Reports each block's loudness to ``on_level`` (drives the orb) when given.

    Returns:
        ``True`` if it played to completion, ``False`` if it was interrupted.
    """
    block = max(1, sample_rate // 30)
    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
    stream.start()
    interrupted = False
    try:
        for start in range(0, audio.size, block):
            if cancel.is_set():
                interrupted = True
                break
            chunk = audio[start : start + block]
            if on_level is not None:
                on_level(rms_level(chunk.astype(np.float32) / 32768.0))
            stream.write(chunk)
    finally:
        if on_level is not None:
            on_level(0.0)  # settle the orb back to quiet
        # abort() drops buffered audio for a snappy stop; stop() drains normally.
        (stream.abort if interrupted else stream.stop)()
        stream.close()
    return not interrupted


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
        self._cancel = threading.Event()  # set by stop() to interrupt playback

    def speak(self, text: str) -> None:
        """Synthesize ``text`` and play it; interruptible via :meth:`stop`."""
        if not text.strip():
            return
        import sounddevice as sd

        self._cancel.clear()  # fresh reply — clear any leftover interrupt
        # piper>=1.2: synthesize() yields one AudioChunk per sentence, each with
        # an int16 numpy array and its sample rate.
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return
        audio = np.concatenate([chunk.audio_int16_array for chunk in chunks])
        sample_rate = int(chunks[0].sample_rate)
        _log.debug("speaking chars=%d samples=%d rate=%d", len(text), audio.size, sample_rate)
        if not play_cancellable(sd, audio, sample_rate, self._cancel, self._on_level):
            _log.info("tts interrupted (barge-in)")

    def stop(self) -> None:
        """Interrupt any in-progress playback (called when the user barges in)."""
        self._cancel.set()
