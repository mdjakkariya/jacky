"""The coder turn: plan → approve → act, driven over a suspend/resume channel.

A coding turn runs on a daemon worker thread that can *park awaiting an answer from
the CLI* and *resume* when it arrives. :class:`TurnChannel` is the two-queue handoff;
:class:`SuspendingConfirmer` is the coder gate's confirmer (it routes mid-act
confirmations to the active turn's channel instead of a TTY it doesn't have).
"""

from __future__ import annotations

import queue
from typing import Any

from autobot.logging_setup import get_logger

_log = get_logger("coder")


class TurnChannel:
    """Two-queue handoff between a parked worker turn and the HTTP layer.

    The worker calls :meth:`ask`/:meth:`done` (producer side); the HTTP handlers call
    :meth:`poll`/:meth:`answer` (consumer side). Both directions block, so the worker's
    Python call stack *is* the turn's continuation — no state is serialized between
    requests.
    """

    def __init__(self) -> None:
        """Create empty out/in queues."""
        self._out: queue.Queue[dict[str, Any]] = queue.Queue()
        self._in: queue.Queue[dict[str, str]] = queue.Queue()

    def ask(self, event: dict[str, Any]) -> dict[str, str]:
        """Worker: surface ``event`` to the HTTP layer, block for the CLI's answer."""
        self._out.put(event)
        return self._in.get()

    def done(self, reply: str) -> None:
        """Worker: surface the final reply and end the turn."""
        self._out.put({"status": "done", "reply": reply})

    def poll(self) -> dict[str, Any]:
        """HTTP: block for the worker's next event (a plan, a pending ask, or done)."""
        return self._out.get()

    def answer(self, value: str, text: str = "") -> None:
        """HTTP: deliver the CLI's answer to the parked worker."""
        self._in.put({"value": value, "text": text})


class SuspendingConfirmer:
    """Coder gate confirmer: suspends the turn to ask the CLI (no TTY of its own).

    The active turn's channel is set by :class:`CoderTurnDriver` via :meth:`set_channel`
    before each act phase. Answers ``"yes"``/``"y"``/``"once"`` proceed; anything else
    (or no active channel) cancels — the gate then reports the action wasn't performed.
    """

    def __init__(self) -> None:
        """Start with no active channel (set per turn by the driver)."""
        self._channel: TurnChannel | None = None

    def set_channel(self, channel: TurnChannel | None) -> None:
        """Point this confirmer at the active turn's channel (or ``None`` between turns)."""
        self._channel = channel

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Ask the CLI via the active channel; ``True`` only on an affirmative answer."""
        if self._channel is None:
            return False  # no active turn to ask — refuse rather than block forever
        answer = self._channel.ask({"status": "pending", "kind": kind, "prompt": prompt})
        return answer.get("value", "").strip().lower() in {"yes", "y", "once"}

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Tri-state confirm for the gate: ``"once"`` on yes, ``""`` (cancel) otherwise."""
        return "once" if self.confirm(prompt, kind) else ""

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        """Coder tools don't use choices — grant the least-privilege default."""
        return default
