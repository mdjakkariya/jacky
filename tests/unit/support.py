"""Shared test doubles for the CLI TUI (no TTY, no daemon)."""

from __future__ import annotations

from collections import deque
from typing import Any

from autobot.cli.classify import Segment
from autobot.cli.prompt import Answer


class FakeSurface:
    """A recording ``Surface``: captures commits/activity, answers ask() from a queue."""

    def __init__(self, answers: list[Answer] | None = None) -> None:
        """Optionally preload ``answers`` returned by successive ``ask`` calls."""
        self.commits: list[Any] = []
        self.activity: list[str] = []
        self.commands: list[tuple[str, list[str]]] = []  # (label, output) per finished command
        self.todos: list[list[tuple[str, str]]] = []  # each set_todos snapshot, in order
        self._answers: deque[Answer] = deque(answers or [])
        self.asked: list[Segment] = []

    def commit(self, renderable: Any) -> None:
        """Record a committed renderable."""
        self.commits.append(renderable)

    def commit_command(self, label: str, output: list[str], *, gated: bool = False) -> None:
        """Record a finished command's compact card + its full output."""
        self.commands.append((label, list(output)))

    def set_activity(self, text: str) -> None:
        """Record an activity-line update."""
        self.activity.append(text)

    def set_todos(self, todos: list[tuple[str, str]]) -> None:
        """Record a live-checklist snapshot."""
        self.todos.append(list(todos))

    def clear_activity(self) -> None:
        """Record a live-region clear (as an empty activity)."""
        self.activity.append("")

    async def ask(self, seg: Segment) -> Answer:
        """Record the gate and return the next preset answer (default: decline)."""
        self.asked.append(seg)
        return self._answers.popleft() if self._answers else Answer("no")
