"""Wake gating at the text level: decide if an utterance is addressed to us.

Two strategies behind one :class:`WakeGate` protocol:

* :class:`PassThroughGate` — every utterance is a command (used when the wake
  word was already handled upstream, e.g. openWakeWord or push-to-talk).
* :class:`SttWakeGate` — the "transcribe-then-match" detector: a transcript is a
  command only if it starts with the wake phrase (which is then stripped), or if
  we're inside the follow-up window after a recent turn. This is what makes a
  wake word work even when spoken continuously ("hey jarvis what's the time"),
  because matching happens on Whisper's text rather than the raw audio.

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


def extract_command(text: str, phrase: str, max_lead_words: int = 4) -> str | None:
    """Strip a leading wake phrase and return the command that follows.

    The wake word matches if the phrase's salient token (its last word, e.g.
    ``"jarvis"``) appears within the first ``max_lead_words`` tokens — tolerant of
    Whisper's leading filler ("hey", "hi", "ok", punctuation).

    Returns:
        The command text after the wake word; ``""`` if only the wake word was
        said; or ``None`` if the wake word isn't present near the start.
    """
    tokens = _TOKEN.findall(text.lower())
    phrase_tokens = _TOKEN.findall(phrase.lower())
    if not phrase_tokens:
        return None
    key = phrase_tokens[-1]
    lead = tokens[:max_lead_words]
    if key not in lead:
        return None
    idx = lead.index(key)
    return " ".join(tokens[idx + 1 :])


@runtime_checkable
class WakeGate(Protocol):
    """Decides whether a transcript is addressed to the assistant."""

    def process(self, text: str) -> WakeResult:
        """Classify one transcript into a :class:`WakeResult`."""
        ...

    def mark_turn_complete(self) -> None:
        """Signal that a turn finished, so a follow-up window can (re)open."""
        ...


class PassThroughGate:
    """Treats every (non-empty) transcript as a command."""

    def process(self, text: str) -> WakeResult:  # noqa: D102 - see class docstring
        return WakeResult(Address.COMMAND, text.strip())

    def mark_turn_complete(self) -> None:  # noqa: D102 - no follow-up state to keep
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

    def _elapsed(self) -> float | None:
        """Seconds since the last completed turn, or ``None`` if there was none."""
        if self._last_turn_at is None:
            return None
        return self._clock() - self._last_turn_at

    def _in_follow_up(self) -> bool:
        elapsed = self._elapsed()
        return self._window > 0 and elapsed is not None and elapsed <= self._window

    def process(self, text: str) -> WakeResult:
        """Accept inside the follow-up window; otherwise require the wake phrase."""
        command = extract_command(text, self._phrase)
        elapsed = self._elapsed()
        # Debug context so the transcript/log show why a turn was (not) accepted.
        detail = (
            f"follow_up={'yes' if self._in_follow_up() else 'no'} "
            f"elapsed={elapsed:.0f}s window={self._window:.0f}s"
            if elapsed is not None
            else f"follow_up=no (first turn) window={self._window:.0f}s"
        )
        if self._in_follow_up():
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
