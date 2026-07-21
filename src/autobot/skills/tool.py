"""The ``skill(name)`` and ``read_skill_file`` tools.

``skill`` is tier-2 activation: it returns the skill's full Markdown body so the
model can follow it. Read-only and ``core`` (always advertised): activation itself
has no side effects; anything the model *does* while following the skill still
flows through the permission gate.

``read_skill_file`` is tier-3: it reads a file the skill *bundles* (e.g.
``references/HEADER.txt``), resolved relative to — and path-jailed to — that
skill's own directory. This exists because ``read_file`` is jailed to the
workspace, not to a skill's directory, so it can't reach a skill's reference files
at all when the skill was discovered from outside the workspace (e.g.
``~/.claude/skills/``).
"""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.skills.registry import SkillRegistry
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

_log = get_logger("skills")

_MAX_SKILL_FILE_CHARS = 20_000


def _resolve_in_skill_dir(base: Path, path: str) -> Path | None:
    """Resolve ``path`` relative to ``base``, or ``None`` if it escapes ``base``.

    Guards against both ``..`` traversal and an absolute ``path`` that would
    otherwise (via ``Path.__truediv__``) discard ``base`` entirely.
    """
    base_resolved = base.resolve()
    target = (base_resolved / path).resolve()
    if target == base_resolved or base_resolved in target.parents:
        return target
    return None


def register_skill_tools(registry: ToolRegistry, skills: SkillRegistry) -> None:
    """Register the ``skill`` and ``read_skill_file`` tools, bound to ``skills``."""

    def _skill(name: str) -> str:
        body = skills.body(name)
        if body is None:
            return ToolFailure(
                f"unknown skill: {name!r}. Use the exact name from the skills catalog.",
                ErrorCategory.NOT_FOUND,
            )
        _log.info("skill activated name=%r", name)
        hint = (
            "\n\n---\nThis skill may bundle files (e.g. references/…). Read them with "
            f'read_skill_file(name="{name}", path="references/…"), not read_file.'
        )
        return body + hint

    def _read_skill_file(name: str, path: str) -> str:
        base = skills.skill_dir(name)
        if base is None:
            return ToolFailure(f"unknown skill: {name!r}", ErrorCategory.NOT_FOUND)
        target = _resolve_in_skill_dir(base, path)
        if target is None:
            return ToolFailure("path escapes the skill directory", ErrorCategory.DENIED)
        if not target.is_file():
            return ToolFailure(f"not found: {path}", ErrorCategory.NOT_FOUND)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolFailure(f"unreadable: {path} ({exc})", ErrorCategory.UNREADABLE)
        if len(text) > _MAX_SKILL_FILE_CHARS:
            text = text[:_MAX_SKILL_FILE_CHARS] + "\n…[truncated]"
        _log.info("skill file read name=%r path=%r", name, path)
        return text

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
    registry.register(
        ToolSpec(
            name="read_skill_file",
            description=(
                "Read a file bundled inside a skill (e.g. a reference doc the skill points "
                "to), by skill name + path relative to the skill. Use this — not read_file — "
                "for any path a skill's instructions mention (e.g. 'references/HEADER.txt')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact skill name from the catalog."},
                    "path": {
                        "type": "string",
                        "description": "File path relative to the skill's own directory.",
                    },
                },
                "required": ["name", "path"],
            },
            handler=_read_skill_file,
            risk=Risk.READ_ONLY,
            core=True,
        )
    )
    _log.info("skill tools registered")
