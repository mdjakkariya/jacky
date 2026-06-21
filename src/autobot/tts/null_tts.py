"""A no-op text-to-speech engine.

Used when voice output is disabled, or when the optional TTS dependency/voice
isn't available — so the rest of the app never has to check whether it can speak.
"""

from __future__ import annotations


class NullTTS:
    """Silently ignores everything it's asked to say."""

    def speak(self, text: str) -> None:  # noqa: D102 - see class docstring
        return

    def stop(self) -> None:  # noqa: D102 - nothing ever plays
        return
