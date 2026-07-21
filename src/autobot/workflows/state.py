"""Process-level pointer to the active :class:`WorkflowRegistry`.

Mirrors ``autobot.skills.state``: ``app.py::build()`` sets the registry once, and the
per-turn prompt assembly reads it back without threading it through every provider
constructor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.workflows.registry import WorkflowRegistry

_active: WorkflowRegistry | None = None


def set_active_workflows(registry: WorkflowRegistry | None) -> None:
    """Set (or clear) the process-wide active workflow registry."""
    global _active
    _active = registry


def active_workflows() -> WorkflowRegistry | None:
    """Return the active workflow registry, or ``None`` if workflows are not wired."""
    return _active
