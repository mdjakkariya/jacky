"""Confirming destructive actions by voice (and, later, a click on the orb card).

The permission gate calls :meth:`Confirmer.confirm` before any destructive tool
runs. :class:`VoiceConfirmer` is the hands-free implementation: it shows a card on
the orb, asks aloud, and listens for a spoken yes/no — auto-cancelling on silence,
ambiguity, or timeout. The safety rule is strict: it returns ``True`` only on an
explicit, un-negated yes; every other outcome cancels, so nothing destructive
happens unless the user clearly approves it.

:func:`parse_confirmation` (pure) is the yes/no/maybe classifier and is unit-tested
directly; the spoken/timed flow is tested with fakes (no mic, no audio).
"""

from __future__ import annotations

import contextlib
import queue
import re
import time
from collections.abc import Callable

from autobot.logging_setup import get_logger

_log = get_logger("gate")


class ConfirmInbox:
    """Thread-safe one-slot mailbox for a confirmation answer clicked on the orb.

    The daemon (asyncio thread) calls :meth:`submit` when the user clicks Yes/No;
    the confirmer (engine thread) polls :meth:`take`. Holds a single pending answer
    — extra clicks while one is queued are ignored.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[bool] = queue.Queue(maxsize=1)

    def submit(self, answer: bool) -> None:
        """Record a clicked answer (no-op if one is already pending)."""
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(answer)

    def take(self) -> bool | None:
        """Return and clear the pending answer, or ``None`` if there isn't one."""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None


_WORD = re.compile(r"[a-z']+")

# Negation is checked first so "yes, but no — wait" cancels. Whole-word matches
# (so "now" never reads as "no"); multi-word cues are substring-matched.
_NO_WORDS = frozenset({"no", "nope", "nah", "cancel", "stop", "abort", "negative", "wait", "dont"})
_NO_PHRASES = ("don't", "do not", "never mind", "nevermind", "leave it", "forget it", "hold on")
_YES_WORDS = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "proceed",
        "confirm",
        "confirmed",
        "affirmative",
        "definitely",
        "absolutely",
    }
)
_YES_PHRASES = ("go ahead", "do it", "go for it", "please do", "of course", "sounds good")


def parse_confirmation(text: str) -> bool | None:
    """Classify a spoken answer: ``True`` = yes, ``False`` = no, ``None`` = unclear.

    No-words win over yes-words (safer for destructive actions), so a mixed answer
    like "yes but actually no" cancels.
    """
    lowered = text.lower()
    tokens = set(_WORD.findall(lowered))
    if tokens & _NO_WORDS or any(p in lowered for p in _NO_PHRASES):
        return False
    if tokens & _YES_WORDS or any(p in lowered for p in _YES_PHRASES):
        return True
    return None


class VoiceConfirmer:
    """Confirm a destructive action by voice, with a card on the orb and a timeout."""

    def __init__(
        self,
        *,
        speak: Callable[[str], None],
        listen: Callable[[float], str],
        on_show: Callable[[str], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        poll_click: Callable[[], bool | None] | None = None,
        flush: Callable[[], None] | None = None,
        timeout_s: float = 30.0,
        chunk_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._speak = speak
        self._listen = listen  # (max_wait_s) -> transcript ("" on silence)
        self._on_show = on_show
        self._on_clear = on_clear
        self._poll_click = poll_click  # () -> True/False from a card click, else None
        self._flush = flush  # drop pre-prompt audio so only the answer is heard
        self._timeout_s = timeout_s
        self._chunk_s = chunk_s
        self._clock = clock

    def _resolve(self, answer: bool, how: str) -> bool:
        """Record the outcome and return it.

        We deliberately don't *speak* the outcome here — the assistant's normal
        reply (driven by the tool result) is the single spoken response, so the
        user doesn't hear two overlapping lines.
        """
        _log.info("confirmation %s by %s", "approved" if answer else "declined", how)
        return answer

    def confirm(self, prompt: str) -> bool:
        """Wait for a clear yes (voice *or* a card click); cancel on anything else.

        Listens in short chunks so a click is picked up within a couple of seconds,
        and cancels on no / silence / ambiguity / timeout — only an explicit,
        un-negated yes proceeds.
        """
        if self._poll_click is not None:
            while self._poll_click() is not None:
                pass  # discard any stale click from a previous prompt
        if self._on_show is not None:
            self._on_show(prompt)
        try:
            _log.info("confirming prompt=%r", prompt)
            self._speak(f"{prompt} Say proceed to confirm, or cancel.")
            # Drop anything captured before/while asking, so only speech that comes
            # *after* the question can answer it (never the command tail or filler).
            if self._flush is not None:
                self._flush()
            deadline = self._clock() + self._timeout_s
            reprompted = False
            while self._clock() < deadline:
                # A click on the card resolves immediately (checked each chunk).
                if self._poll_click is not None:
                    clicked = self._poll_click()
                    if clicked is not None:
                        return self._resolve(clicked, "click")
                chunk = min(self._chunk_s, max(0.1, deadline - self._clock()))
                text = self._listen(chunk)
                if not text.strip():
                    continue  # silence — keep waiting (and re-checking for a click)
                answer = parse_confirmation(text)
                _log.info("confirm heard=%r -> %s", text, answer)
                if answer is not None:
                    return self._resolve(answer, "voice")
                if not reprompted:  # heard something unclear — re-ask once
                    reprompted = True
                    self._speak("Sorry, was that a yes or no?")
            _log.info("confirmation timed out — cancelling")
            return False  # the assistant's reply will note it wasn't done
        finally:
            if self._on_clear is not None:
                self._on_clear()
