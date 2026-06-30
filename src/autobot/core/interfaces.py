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
    from autobot.tools.registry import ToolSpec


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
        Playback must be interruptible: if :meth:`stop` is called from another
        thread while speaking, return promptly instead of finishing the audio.
        """
        ...

    def stop(self) -> None:
        """Request that any in-progress playback stop as soon as possible.

        Called from another thread (e.g. when the user barges in). Safe to call
        when nothing is playing — it just clears the way for the next ``speak``.
        """
        ...


@runtime_checkable
class ToolSelector(Protocol):
    """Chooses which tools to advertise to the model for one round.

    The pipeline funnels every request's tool list through a selector instead of
    advertising the whole registry. Implementations return the always-on core set
    plus the gated tools judged relevant to ``query`` (and any explicitly
    ``pinned`` tools), bounded so per-turn tool context stays small.
    """

    def select(self, query: str, *, pinned: frozenset[str] = frozenset()) -> list[ToolSpec]:
        """Return the ToolSpecs to advertise this round.

        Args:
            query: The current user message (the relevance signal).
            pinned: Tool names to force-include (e.g. discovered via an escape
                hatch); resolved against the registry and added to the result.

        Returns:
            A bounded, deduplicated list of specs: core U pinned U top relevant.
        """
        ...

    def search(self, intent: str, *, limit: int = 5) -> list[str]:
        """Return the names of the best gated tools for an explicit intent.

        The model's escape hatch: when the relevance-gated set advertised by
        :meth:`select` lacks the tool a request needs, the model calls
        ``find_tools(intent)`` and the turn loop forwards ``intent`` here. The
        returned names are then pinned (force-advertised via ``select(..., pinned)``)
        for the rest of the turn, so the model can call the real tool next round.

        Args:
            intent: A short natural-language description of what the model wants to
                do (e.g. ``"send a message on slack"``).
            limit: Maximum number of tool names to return.

        Returns:
            Up to ``limit`` bare tool names, most relevant first. Never includes
            always-on core tools (the model already sees those). Empty when nothing
            matches.
        """
        ...
