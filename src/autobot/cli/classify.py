"""Map a daemon status dict to a semantic :class:`Segment` the renderers switch on.

Kept separate from rendering so a plain renderer and a rich renderer share one
classification, and so the streaming slice can classify event dicts the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Segment:
    """One renderable unit of a turn: its kind plus the payload each kind needs."""

    kind: str
    text: str = ""
    todo: tuple[str, ...] = ()


def classify(event: dict[str, Any]) -> Segment:
    """Classify a daemon dict: streaming (``type``) or phase (``status``) event."""
    etype = event.get("type")
    if etype == "token":
        return Segment("token", str(event.get("text", "")))
    if etype == "tool":
        return Segment("tool", str(event.get("label", event.get("name", ""))))
    if etype == "output":
        return Segment("output", str(event.get("text", "")))
    status = event.get("status", "")
    if status == "plan":
        todo = tuple(str(s) for s in (event.get("todo") or []))
        return Segment("plan", str(event.get("reply", "")), todo)
    if status == "pending":
        return Segment("pending", str(event.get("prompt", "Proceed?")))
    if status == "error":
        return Segment("error", str(event.get("reply", "Something went wrong.")))
    return Segment("done", str(event.get("reply", "")))
