"""Wake gating at the text level: decide if an utterance is addressed to us.

Two strategies behind one :class:`WakeGate` protocol:

* :class:`PassThroughGate` — every utterance is a command (used when the wake
  word was already handled upstream, e.g. openWakeWord or push-to-talk).
* :class:`SttWakeGate` — the "transcribe-then-match" detector: a transcript is a
  command if the wake phrase appears anywhere in it (and is stripped out), or if
  we're inside the follow-up window after a recent turn. Matching on Whisper's text
  (not the raw audio) is what lets the wake word work spoken continuously and in any
  position — "hey jarvis what's the time", "open spotify jarvis", or mid-sentence
  with several actions ("open spotify jarvis and also close vscode").

The matching logic (:func:`extract_command`) is a pure function, easy to test.
"""

from __future__ import annotations

import enum
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_TOKEN = re.compile(r"[a-z0-9']+")

# Filler that may precede the wake word in a *leading* address ("hey jack…",
# "ok jarvis…"). Anything else before the wake word means it's not a lead-in.
_LEAD_FILLER = frozenset({"hey", "hi", "hello", "ok", "okay", "um", "uh", "yo", "so", "well"})


class Address(enum.Enum):
    """Whether/how an utterance is addressed to the assistant."""

    COMMAND = "command"  # addressed, with a command to run
    GREETED = "greeted"  # addressed by the wake word alone ("hey jarvis")
    IGNORED = "ignored"  # not addressed — no wake word, not in a follow-up


@dataclass(frozen=True, slots=True)
class WakeResult:
    """The outcome of gating one transcript."""

    address: Address
    command: str = ""  # the command text (empty for GREETED / IGNORED)
    detail: str = ""  # debug context, e.g. follow-up window timing


def extract_command(text: str, phrase: str) -> str | None:
    """Detect the wake word anywhere in the transcript and return the command.

    People address the assistant in every position — "Jack, open Spotify", "open
    Spotify, jack", and even mid-sentence with several actions ("open spotify jack
    and also close vscode"). So if the salient token (the phrase's last word, e.g.
    ``"jack"``) appears *anywhere*, the utterance is addressed; the command is the
    transcript with the wake word removed and any leading filler ("hey", "ok")
    trimmed. Matching is whole-token, so "jacket"/"hijack" don't trigger it.

    Returns:
        The command text (wake word removed); ``""`` if only the wake word (± filler)
        was said; or ``None`` if the wake word isn't present at all.
    """
    tokens = _TOKEN.findall(text.lower())
    phrase_tokens = _TOKEN.findall(phrase.lower())
    if not phrase_tokens:
        return None
    key = phrase_tokens[-1]
    if key not in tokens:
        return None
    command = [t for t in tokens if t != key]
    while command and command[0] in _LEAD_FILLER:
        command.pop(0)  # drop a leftover "hey"/"ok" lead-in
    return " ".join(command)


@runtime_checkable
class WakeGate(Protocol):
    """Decides whether a transcript is addressed to the assistant."""

    def process(self, text: str, started_at: float | None = None) -> WakeResult:
        """Classify one transcript into a :class:`WakeResult`."""
        ...

    def mark_turn_complete(self) -> None:
        """Signal that a turn finished, so a follow-up window can (re)open."""
        ...

    def end_follow_up(self) -> None:
        """Close any follow-up window so the next utterance needs the wake word."""
        ...


class PassThroughGate:
    """Treats every (non-empty) transcript as a command."""

    def process(self, text: str, started_at: float | None = None) -> WakeResult:  # noqa: D102
        return WakeResult(Address.COMMAND, text.strip())

    def mark_turn_complete(self) -> None:  # noqa: D102 - no follow-up state to keep
        pass

    def end_follow_up(self) -> None:  # noqa: D102 - no follow-up state to keep
        pass


class SttWakeGate:
    """Requires the wake phrase in the transcript, except inside the follow-up window."""

    def __init__(
        self,
        wake_phrase: str,
        follow_up_window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._phrase = wake_phrase
        self._window = follow_up_window_s
        self._clock = clock
        self._last_turn_at: float | None = None

    def _elapsed(self, started_at: float | None = None) -> float | None:
        """Seconds from the last completed turn to when this utterance *began*.

        Measured against ``started_at`` (the moment the user started speaking) when
        available, not "now" — otherwise a long utterance or slow transcription
        could push a turn that began inside the window past it, dropping the reply.
        Falls back to the current time when no speech-start time is given.
        """
        if self._last_turn_at is None:
            return None
        reference = started_at if started_at is not None else self._clock()
        return reference - self._last_turn_at

    def process(self, text: str, started_at: float | None = None) -> WakeResult:
        """Accept inside the follow-up window; otherwise require the wake phrase."""
        command = extract_command(text, self._phrase)
        elapsed = self._elapsed(started_at)
        in_follow_up = self._window > 0 and elapsed is not None and elapsed <= self._window
        # Debug context so the transcript/log show why a turn was (not) accepted.
        detail = (
            f"follow_up={'yes' if in_follow_up else 'no'} "
            f"elapsed={elapsed:.0f}s window={self._window:.0f}s"
            if elapsed is not None
            else f"follow_up=no (first turn) window={self._window:.0f}s"
        )
        if in_follow_up:
            # No wake word needed; accept the whole utterance (or the stripped
            # command if the user said the wake word again).
            if command:
                return WakeResult(Address.COMMAND, command, detail)
            if command == "":  # wake word said with nothing after it
                return WakeResult(Address.GREETED, detail=detail)
            return WakeResult(Address.COMMAND, text.strip(), detail)
        if command is None:
            return WakeResult(Address.IGNORED, detail=detail)
        if command == "":
            return WakeResult(Address.GREETED, detail=detail)
        return WakeResult(Address.COMMAND, command, detail)

    def mark_turn_complete(self) -> None:
        """Open/refresh the follow-up window from now."""
        self._last_turn_at = self._clock()

    def end_follow_up(self) -> None:
        """Forget the last turn so the follow-up window won't accept the next one.

        Used when the user dismisses the assistant ("go away"): coming back should
        require the wake word, not a lingering follow-up window.
        """
        self._last_turn_at = None
