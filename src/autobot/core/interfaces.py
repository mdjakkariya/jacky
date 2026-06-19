"""Structural interfaces (``Protocol``s) for the swappable pipeline stages.

Each stage of the assistant — capture audio, transcribe, reason — is defined
here as a :class:`typing.Protocol`. Concrete implementations live in their own
subpackages (:mod:`autobot.io`, :mod:`autobot.stt`, :mod:`autobot.llm`) and are
wired together in :mod:`autobot.app`. Because the wiring depends only on these
protocols, swapping faster-whisper for Moonshine, or Ollama for another runtime,
is a one-line change in the factory — nothing else needs to know.

These protocols are ``runtime_checkable`` so tests can assert that a fake
satisfies the contract with ``isinstance``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Imported only for type checking so this module stays runtime-light.
    from autobot.core.types import AudioClip, ToolExecutor, Transcription


@runtime_checkable
class AudioSource(Protocol):
    """Produces a single audio clip to be transcribed.

    Phase 0 implements this as push-to-talk; Phase 2 swaps in a wake-word +
    VAD source with the same contract, so downstream stages are unaffected.
    """

    def record_clip(self) -> AudioClip:
        """Block until one utterance is captured.

        Returns:
            A 1-D ``float32`` array of mono PCM samples at 16 kHz. May be empty
            if nothing was captured.
        """
        ...


@runtime_checkable
class SpeechToText(Protocol):
    """Converts an audio clip into text (English only)."""

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Transcribe one mono ``float32`` clip at 16 kHz.

        Args:
            audio: Mono PCM samples as produced by an :class:`AudioSource`.

        Returns:
            A :class:`~autobot.core.types.Transcription`.
        """
        ...


@runtime_checkable
class LanguageModel(Protocol):
    """Plans and answers a user turn, invoking tools as needed."""

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Handle one user utterance end-to-end.

        Implementations advertise the registered tools to the model, run any tool
        calls it returns **through the provided executor** (never directly), feed
        the results back, and return the model's final natural-language reply.
        Routing execution through ``execute`` is what lets the permission gate sit
        between planning and side effects.

        Args:
            user_text: The transcribed user request, in English.
            execute: Callback that runs one tool call and returns its result —
                wired by the orchestrator to the permission gate.

        Returns:
            The assistant's final reply text.
        """
        ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Speaks a reply aloud (English, on-device)."""

    def speak(self, text: str) -> None:
        """Synthesize ``text`` and play it, blocking until playback finishes.

        Implementations should no-op on empty text. A disabled or unavailable
        engine is represented by a null implementation, so callers never branch.
        """
        ...
