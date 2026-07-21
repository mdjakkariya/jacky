"""The ``skill(name)`` tool — tier-2 activation of a discovered skill.

Returns the skill's full Markdown body so the model can follow it. Read-only and
``core`` (always advertised): activation itself has no side effects; anything the
model *does* while following the skill still flows through the permission gate.
"""

from __future__ import annotations

from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.skills.registry import SkillRegistry
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

_log = get_logger("skills")


def register_skill_tools(registry: ToolRegistry, skills: SkillRegistry) -> None:
    """Register the ``skill`` activation tool, bound to ``skills``."""

    def _skill(name: str) -> str:
        body = skills.body(name)
        if body is None:
            return ToolFailure(
                f"unknown skill: {name!r}. Use the exact name from the skills catalog.",
                ErrorCategory.NOT_FOUND,
            )
        _log.info("skill activated name=%r", name)
        return body

    registry.register(
        ToolSpec(
            name="skill",
            description=(
                "Load a skill's full instructions by name and follow them. Call this the "
                "moment the task matches a skill listed under 'Available skills' — pass the "
                "exact skill name. Returns the skill's step-by-step guidance; prefer a "
                "matching skill over improvising."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact skill name from the catalog."}
                },
                "required": ["name"],
            },
            handler=_skill,
            risk=Risk.READ_ONLY,
            core=True,
        )
    )
    _log.info("skill tool registered")
