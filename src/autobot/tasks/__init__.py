"""The generic async-task primitive: a registry of off-the-main-turn work + an inbox.

This is the shared foundation behind two features (see
``docs/architecture/design-reference.md`` and the roadmap issue): running a command
off the turn (``kind="command"``, built first) and — later — running subagents
concurrently (``kind="agent"``). Both reduce to the same shape: a unit of work that runs
off the main turn and whose completion is delivered back as a *notification* that
re-engages the agent.

Two pieces, deliberately decoupled so neither knows how the other is wired:

* :class:`~autobot.tasks.registry.TaskRegistry` — a thread-safe, process-global store of
  task rows (``running`` → ``done``/``failed``). It only records state; it does not decide
  who gets told.
* :class:`~autobot.tasks.inbox.NotificationInbox` — a per-session queue of completion
  notes. The worker that finishes a task marks the registry *and* pushes a note here; the
  agent drains its session's notes at the start of the next turn.
"""

from __future__ import annotations

from autobot.tasks.inbox import NotificationInbox
from autobot.tasks.registry import Task, TaskRegistry

__all__ = ["NotificationInbox", "Task", "TaskRegistry"]
