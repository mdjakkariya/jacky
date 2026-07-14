"""A thread-safe, process-global registry of async tasks (the "build once" primitive).

A :class:`Task` is one unit of work that runs off the main turn. Today the only kind is
``"command"`` (a backgrounded shell command); ``"agent"`` (a subagent turn) is the planned
second kind and needs no change here — the registry is kind-agnostic.

The registry is *just a store*: it records rows and moves them through
``running`` → ``done``/``failed``. It is process-global (it lives in the daemon), so a task
started in one turn is still tracked in the next. It never spawns work, never talks to a
model, and never decides who is notified — that coordination lives in the caller (which
also pushes to the :class:`~autobot.tasks.inbox.NotificationInbox`). Every method holds an
internal lock, so background completion threads and the turn thread can touch it freely.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

TaskKind = Literal["command", "agent"]
TaskStatus = Literal["running", "done", "failed"]

_DEFAULT_MAX_TASKS = 200  # bound the store; settled rows are evicted oldest-first past this


@dataclass(frozen=True, slots=True)
class Task:
    """One unit of off-the-turn work and its outcome (immutable; updated by replacement)."""

    id: str
    kind: TaskKind
    session_id: str
    label: str
    status: TaskStatus = "running"
    started: float = 0.0
    finished: float | None = None
    returncode: int | None = None
    result: str = ""  # short summary/excerpt of the outcome, delivered on completion

    @property
    def settled(self) -> bool:
        """Whether the task has finished (``done`` or ``failed``)."""
        return self.status != "running"


class TaskRegistry:
    """A bounded, thread-safe map of task id → :class:`Task`.

    Ids are monotonic (``task-1``, ``task-2``, …) so they read cleanly in logs and the CLI.
    """

    def __init__(
        self, *, now: Callable[[], float] = time.time, max_tasks: int = _DEFAULT_MAX_TASKS
    ) -> None:
        """Create an empty registry.

        Args:
            now: Clock used to stamp ``started``/``finished`` (injectable for tests).
            max_tasks: Soft cap; once exceeded, the oldest *settled* rows are evicted (a
                still-running task is never evicted).
        """
        self._now = now
        self._max = max(1, max_tasks)
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._counter = 0
        self._listeners: list[Callable[[Task], None]] = []

    def add_listener(self, listener: Callable[[Task], None]) -> Callable[[], None]:
        """Call ``listener(task)`` each time a task *settles* (done/failed); return unsubscribe.

        Used by the daemon to push a completion event onto the persistent ``/coder/events``
        stream so an idle CLI can pick the result up. Listeners fire on the completing
        (background) thread, outside the registry lock, and their exceptions are swallowed —
        a bad listener can never wedge the registry or lose a task update.
        """
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def add(self, *, kind: TaskKind, session_id: str, label: str) -> Task:
        """Register a new ``running`` task and return it (with its freshly-minted id)."""
        with self._lock:
            self._counter += 1
            task = Task(
                id=f"task-{self._counter}",
                kind=kind,
                session_id=session_id,
                label=label,
                status="running",
                started=self._now(),
            )
            self._tasks[task.id] = task
            self._evict_locked()
            return task

    def mark_done(self, task_id: str, *, result: str, returncode: int | None = None) -> Task | None:
        """Mark ``task_id`` finished successfully; return the updated task (``None`` if gone)."""
        return self._finish(task_id, status="done", result=result, returncode=returncode)

    def mark_failed(
        self, task_id: str, *, result: str, returncode: int | None = None
    ) -> Task | None:
        """Mark ``task_id`` finished with a failure; return the updated task (``None`` if gone)."""
        return self._finish(task_id, status="failed", result=result, returncode=returncode)

    def get(self, task_id: str) -> Task | None:
        """The current row for ``task_id``, or ``None`` if unknown/evicted."""
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, *, session_id: str | None = None) -> list[Task]:
        """All tasks (optionally only ``session_id``'s), newest first."""
        with self._lock:
            tasks = list(self._tasks.values())
        if session_id is not None:
            tasks = [t for t in tasks if t.session_id == session_id]
        return sorted(tasks, key=lambda t: t.started, reverse=True)

    def running_count(self, *, session_id: str | None = None) -> int:
        """How many tasks are still running (optionally scoped to ``session_id``)."""
        with self._lock:
            return sum(
                1
                for t in self._tasks.values()
                if t.status == "running" and (session_id is None or t.session_id == session_id)
            )

    def _finish(
        self, task_id: str, *, status: TaskStatus, result: str, returncode: int | None
    ) -> Task | None:
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing is None:
                return None
            updated = replace(
                existing,
                status=status,
                result=result,
                returncode=returncode,
                finished=self._now(),
            )
            self._tasks[task_id] = updated
            listeners = list(self._listeners)  # snapshot under lock, fire outside it
        for listener in listeners:
            with contextlib.suppress(Exception):  # a bad listener must not lose the update
                listener(updated)
        return updated

    def _evict_locked(self) -> None:
        """Drop oldest *settled* rows while over the cap. Caller holds ``self._lock``."""
        if len(self._tasks) <= self._max:
            return
        settled = [t for t in self._tasks.values() if t.settled]
        settled.sort(key=lambda t: (t.finished or 0.0, t.started))
        for task in settled:
            if len(self._tasks) <= self._max:
                break
            del self._tasks[task.id]
