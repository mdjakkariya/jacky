"""Process-level pointer to the active :class:`SkillRegistry`.

Mirrors ``autobot.tools.access.active_policy``: ``app.py::build()`` sets the
registry once, and the per-turn prompt assembly reads it back without threading it
through every provider constructor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autobot.skills.registry import SkillRegistry

_active: SkillRegistry | None = None


def set_active_skills(registry: SkillRegistry | None) -> None:
    """Set (or clear) the process-wide active skill registry."""
    global _active
    _active = registry


def active_skills() -> SkillRegistry | None:
    """Return the active skill registry, or ``None`` if skills are not wired."""
    return _active
