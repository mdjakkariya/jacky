"""A per-session queue of completion notes — how a finished task re-engages the agent.

When a background task finishes, the worker pushes a short note here keyed by the session
that started it (e.g. ``"Background command task-3 finished (ok): <excerpt>"``). At the top
of that session's next turn the agent drains its notes and folds them into the turn's
context, so the model acts on the result without the user re-prompting.

This is the neutral seam the design calls the "notification inbox": it carries plain
strings, so it works the same whether the note describes a command result (now) or a
subagent's return value (later). It never blocks and never raises; a bad note is just a
string.
"""

from __future__ import annotations

import threading


class NotificationInbox:
    """Thread-safe map of session id → pending completion notes (FIFO per session)."""

    def __init__(self) -> None:
        """Create an empty inbox."""
        self._lock = threading.Lock()
        self._pending: dict[str, list[str]] = {}

    def push(self, session_id: str, note: str) -> None:
        """Queue ``note`` for ``session_id`` (delivered on its next :meth:`drain`)."""
        with self._lock:
            self._pending.setdefault(session_id, []).append(note)

    def drain(self, session_id: str) -> list[str]:
        """Remove and return all pending notes for ``session_id`` (``[]`` if none)."""
        with self._lock:
            return self._pending.pop(session_id, [])

    def pending(self, session_id: str) -> int:
        """How many notes are waiting for ``session_id`` (without draining them)."""
        with self._lock:
            return len(self._pending.get(session_id, []))
