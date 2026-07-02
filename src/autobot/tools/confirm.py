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
    """Thread-safe one-slot mailbox for a clicked answer to a card.

    The daemon (asyncio thread) calls :meth:`submit` with the clicked value
    ("yes"/"no" for a confirm, or an access level like "read"/"write" for a grant
    choice); the confirmer (engine thread) polls :meth:`take`. Holds a single pending
    answer — extra clicks while one is queued are ignored.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue(maxsize=1)

    def submit(self, value: str) -> None:
        """Record a clicked answer value (no-op if one is already pending)."""
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(value)

    def take(self) -> str | None:
        """Return and clear the pending answer, or ``None`` if there isn't one."""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None


# Values that mean "cancel/decline" when they come back from a card or inbox.
_CANCEL_VALUES = frozenset({"", "no", "cancel"})


def _value_to_bool(value: str | None) -> bool | None:
    """Map a clicked value to yes/no for confirm(): None passes through unchanged."""
    if value is None:
        return None
    return value not in _CANCEL_VALUES


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
        "allow",
        "proceed",
        "confirm",
        "confirmed",
        "affirmative",
        "definitely",
        "absolutely",
    }
)
_YES_PHRASES = ("go ahead", "do it", "go for it", "please do", "of course", "sounds good")

_GRANT_OPTIONS: list[dict[str, str]] = [
    {"label": "Allow once", "value": "once"},
    {"label": "Allow this session", "value": "session"},
]
# Spoken cues that mean "grant this for the rest of the session", not just once.
_SESSION_CUES = ("for all", "this session", "every time", "always", "don't ask", "dont ask")


def _session_cue(text: str) -> bool:
    """Whether a spoken answer asks to remember the grant for the session."""
    lowered = text.lower()
    return any(cue in lowered for cue in _SESSION_CUES)


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
        on_show: Callable[[str, str, list[dict[str, str]] | None], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        poll_answer: Callable[[], str | None] | None = None,
        flush: Callable[[], None] | None = None,
        is_chat: Callable[[], bool] | None = None,
        timeout_s: float = 30.0,
        chunk_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._speak = speak
        self._listen = listen  # (max_wait_s) -> transcript ("" on silence)
        self._on_show = on_show
        self._on_clear = on_clear
        self._poll_answer = poll_answer  # () -> clicked value string, else None
        self._flush = flush  # drop pre-prompt audio so only the answer is heard
        # When this returns True (chat mode), confirm by the card click only — don't
        # speak the prompt or listen on the mic (which would talk over chat / fight
        # the idle voice loop). Defaults to voice behaviour.
        self._is_chat = is_chat
        self._timeout_s = timeout_s
        self._chunk_s = chunk_s
        self._clock = clock
        self._sleep = sleep
        # Whether the most recent confirm() ended by *timing out* (no answer) rather
        # than an explicit cancel. Lets the gate phrase "I cancelled because I didn't
        # get a confirmation" instead of treating silence like a deliberate "no".
        self._timed_out = False

    @property
    def timed_out(self) -> bool:
        """True if the last :meth:`confirm` ended by timeout (no answer given)."""
        return self._timed_out

    def _confirm_by_click(self, prompt: str) -> bool:
        """Chat-mode confirm: poll the card click until answered or timed out."""
        _log.info("confirming (chat, click-only) prompt=%r", prompt)
        deadline = self._clock() + self._timeout_s
        while self._clock() < deadline:
            if self._poll_answer is not None:
                clicked = _value_to_bool(self._poll_answer())
                if clicked is not None:
                    return self._resolve(clicked, "click")
            self._sleep(min(0.15, self._chunk_s))
        _log.info("confirmation timed out (chat) — cancelling")
        self._timed_out = True
        return False

    def _resolve(self, answer: bool, how: str) -> bool:
        """Record the outcome and return it.

        We deliberately don't *speak* the outcome here — the assistant's normal
        reply (driven by the tool result) is the single spoken response, so the
        user doesn't hear two overlapping lines.
        """
        _log.info("confirmation %s by %s", "approved" if answer else "declined", how)
        return answer

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        """Ask the user to pick an option (e.g. an access level); "" means cancel.

        Chat shows the card with a dropdown and waits for the clicked value; by voice
        a clear yes selects ``default`` (least privilege) and a no cancels. Returns the
        chosen option's value, or "" on cancel / silence / timeout.
        """
        self._timed_out = False
        valid = {o["value"] for o in options}
        if self._poll_answer is not None:
            while self._poll_answer() is not None:
                pass  # drain any stale answer
        if self._on_show is not None:
            self._on_show(prompt, kind, options)
        try:
            deadline = self._clock() + self._timeout_s
            chat = self._is_chat is not None and self._is_chat()
            if not chat:
                self._speak(f"{prompt} Say allow to confirm, or cancel.")
                if self._flush is not None:
                    self._flush()
            reprompted = False
            while self._clock() < deadline:
                if self._poll_answer is not None:
                    v = self._poll_answer()
                    if v is not None:
                        if v in _CANCEL_VALUES:
                            return ""
                        return v if v in valid else default
                if chat:
                    self._sleep(min(0.15, self._chunk_s))
                    continue
                chunk = min(self._chunk_s, max(0.1, deadline - self._clock()))
                text = self._listen(chunk)
                if not text.strip():
                    continue
                if (
                    "session" in valid
                    and _session_cue(text)
                    and parse_confirmation(text) is not False
                ):
                    return "session"
                ans = parse_confirmation(text)
                if ans is True:
                    return default
                if ans is False:
                    return ""
                if not reprompted:
                    reprompted = True
                    self._speak("Sorry, was that a yes or no?")
            self._timed_out = True
            return ""
        finally:
            if self._on_clear is not None:
                self._on_clear()

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Wait for a clear yes (voice *or* a card click); cancel on anything else.

        ``kind`` ("read"/"write"/"danger") only tiers the card's tone; the answer
        logic is unchanged. Listens in short chunks so a click is picked up within a
        couple of seconds, and cancels on no / silence / ambiguity / timeout — only
        an explicit, un-negated yes proceeds.
        """
        self._timed_out = False  # reset; set True only if we end by timeout
        if self._poll_answer is not None:
            while self._poll_answer() is not None:
                pass  # discard any stale click from a previous prompt
        if self._on_show is not None:
            self._on_show(prompt, kind, None)
        try:
            # Chat mode: just show the card and wait for a click — no speaking, no
            # mic (those belong to voice mode and would talk over / fight chat).
            if self._is_chat is not None and self._is_chat():
                return self._confirm_by_click(prompt)
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
                if self._poll_answer is not None:
                    clicked = _value_to_bool(self._poll_answer())
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
            self._timed_out = True
            return False  # the assistant's reply will note it wasn't done
        finally:
            if self._on_clear is not None:
                self._on_clear()

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Confirm a gated action, offering an "Allow this session" grant.

        Reuses :meth:`choose` (card + inbox + voice), so a click picks a button and a
        spoken plain "yes" grants ``"once"`` while a session cue ("for all", "this
        session") grants ``"session"``. Returns "" on cancel / silence / timeout.
        """
        return self.choose(prompt, _GRANT_OPTIONS, kind, default="once")
