"""Piper text-to-speech: fast, on-device English synthesis (CPU).

The heavy ``piper`` runtime and ``sounddevice`` are imported lazily so importing
this module stays cheap. The voice model (an ``.onnx`` file plus its ``.json``
config) is downloaded once by the user and pointed to via ``settings.tts_voice``
(see the README). Swapping engines later (e.g. Kokoro) means writing a sibling
class with the same :meth:`speak` method — nothing else changes.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from autobot.config import Settings
from autobot.core.events import AmplitudeSink
from autobot.core.types import Int16Frame
from autobot.io.endpointing import rms_level
from autobot.logging_setup import get_logger

_log = get_logger("tts")

# Emoji and other pictographs read fine in chat but make Piper stumble (or voice a
# literal name). Strip them before synthesis so the same text can be friendly on
# screen and clean when spoken.
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff"  # emoji & pictographs
    "\U00002600-\U000027bf"  # misc symbols & dingbats
    "\U00002190-\U000021ff"  # arrows
    "\U00002300-\U000023ff"  # technical (incl. ⏳ ⌀)
    "\U0000fe0f\U0000200d]"  # variation selector + zero-width joiner
)


def _strip_for_speech(text: str) -> str:
    """Remove emoji/pictographs and tidy whitespace so TTS speaks clean prose."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text)).strip()


@runtime_checkable
class AudioPlayer(Protocol):
    """Plays a block of int16 PCM, interruptibly. See :func:`play_cancellable`."""

    def play(
        self,
        audio: Int16Frame,
        sample_rate: int,
        cancel: threading.Event,
        on_level: AmplitudeSink | None = None,
    ) -> bool:
        """Play ``audio``; return True if it finished, False if ``cancel`` interrupted."""
        ...


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


class SoundDevicePlayer:
    """Default player: the system default output via sounddevice (no echo cancel)."""

    def play(
        self,
        audio: Int16Frame,
        sample_rate: int,
        cancel: threading.Event,
        on_level: AmplitudeSink | None = None,
    ) -> bool:
        """Play through sounddevice; see :func:`play_cancellable`."""
        import sounddevice as sd

        return play_cancellable(sd, audio, sample_rate, cancel, on_level)


class PiperTTS:
    """Speaks replies with a Piper voice, playing audio through an injected player.

    The ``player`` is where the audio goes: by default the system output
    (:class:`SoundDevicePlayer`), but on the AEC path it's the Voice-Processing
    engine's output node — so macOS cancels Jack's own voice from the mic and
    barge-in works on speakers.
    """

    def __init__(
        self,
        settings: Settings,
        on_level: AmplitudeSink | None = None,
        player: AudioPlayer | None = None,
    ) -> None:
        from piper import PiperVoice

        voice_path = Path(settings.tts_voice).expanduser()
        if not voice_path.exists():
            raise FileNotFoundError(f"Piper voice model not found: {voice_path}")
        _log.info("loading piper voice=%s", voice_path)
        self._voice = PiperVoice.load(str(voice_path))
        self._on_level = on_level
        self._player: AudioPlayer = player or SoundDevicePlayer()
        self._cancel = threading.Event()  # set by stop() to interrupt playback

    def speak(self, text: str) -> None:
        """Synthesize ``text`` and play it; interruptible via :meth:`stop`."""
        text = _strip_for_speech(text)
        if not text:
            return
        self._cancel.clear()  # fresh reply — clear any leftover interrupt
        # piper>=1.2: synthesize() yields one AudioChunk per sentence, each with
        # an int16 numpy array and its sample rate.
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return
        audio = np.concatenate([chunk.audio_int16_array for chunk in chunks])
        sample_rate = int(chunks[0].sample_rate)
        _log.debug("speaking chars=%d samples=%d rate=%d", len(text), audio.size, sample_rate)
        if not self._player.play(audio, sample_rate, self._cancel, self._on_level):
            _log.info("tts interrupted (barge-in)")

    def stop(self) -> None:
        """Interrupt any in-progress playback (called when the user barges in)."""
        self._cancel.set()
