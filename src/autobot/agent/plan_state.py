"""The pure per-turn task checklist the coder act phase is driven by.

:class:`PlanState` holds the living todo list a coding turn works through: it is
seeded from the approved plan (all steps ``pending``) and then replaced by the
model's ``update_plan`` payloads as work progresses. It carries no I/O and no
dependencies, so the driver's completion logic (all steps settled → done) is
unit-tested without a model or a daemon.
"""

from __future__ import annotations

from dataclasses import dataclass

_STATUSES = ("pending", "in_progress", "done", "blocked")
_OPEN = ("pending", "in_progress")
_SETTLED = ("done", "blocked")


@dataclass(frozen=True, slots=True)
class TodoItem:
    """One checklist step and its status (``pending``/``in_progress``/``done``/``blocked``)."""

    step: str
    status: str


class PlanState:
    """A turn's living todo list: seeded pending, then replaced by the model's updates."""

    def __init__(self, steps: list[str]) -> None:
        self._items: list[TodoItem] = [TodoItem(step, "pending") for step in steps]
        self._used: bool = False

    def replace(self, todos: list[dict[str, str]]) -> None:
        """Replace the checklist from an ``update_plan`` payload.

        A non-empty payload marks the state as :meth:`used` and rebuilds the items:
        each entry needs a ``step`` (entries without one are skipped) and a ``status``
        (missing or unrecognised statuses are coerced to ``pending``). An empty payload
        is a no-op, leaving both the items and the used flag untouched.

        Args:
            todos: The model's full checklist as a list of ``{"step", "status"}`` dicts.
        """
        if not todos:
            return
        self._used = True
        items: list[TodoItem] = []
        for todo in todos:
            step = todo.get("step")
            if not step:
                continue
            status = todo.get("status", "pending")
            if status not in _STATUSES:
                status = "pending"
            items.append(TodoItem(step, status))
        self._items = items

    @property
    def items(self) -> list[TodoItem]:
        """The current checklist, in order."""
        return list(self._items)

    def pending(self) -> list[TodoItem]:
        """The still-open items (status ``pending`` or ``in_progress``)."""
        return [item for item in self._items if item.status in _OPEN]

    def all_settled(self) -> bool:
        """Whether every item is ``done`` or ``blocked`` (vacuously true when empty)."""
        return all(item.status in _SETTLED for item in self._items)

    def used(self) -> bool:
        """Whether the model has called ``update_plan`` at least once this turn."""
        return self._used

    def summary(self) -> str:
        """A ``"{done}/{total} done"`` line for logs and tool acks."""
        done = sum(1 for item in self._items if item.status == "done")
        return f"{done}/{len(self._items)} done"

    def remaining_text(self) -> str:
        """The open steps as ``"- {step}"`` lines joined by newlines, for a continue nudge."""
        return "\n".join(f"- {item.step}" for item in self.pending())
