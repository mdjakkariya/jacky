"""Text-to-speech implementations (English, on-device)."""

from __future__ import annotations

from autobot.tts.null_tts import NullTTS
from autobot.tts.piper_tts import PiperTTS

__all__ = ["NullTTS", "PiperTTS"]
