"""The skill() activation tool: returns a body, fails cleanly on unknown names."""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import ErrorCategory
from autobot.skills.registry import SkillDir, SkillRegistry
from autobot.skills.tool import register_skill_tools
from autobot.tools.registry import ToolRegistry


def _reg(tmp_path: Path) -> ToolRegistry:
    d = tmp_path / "pdf-tools"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: Extract PDF text.\n---\n\nStep one: open it.",
        encoding="utf-8",
    )
    skills = SkillRegistry([SkillDir(tmp_path, "user", 20)])
    registry = ToolRegistry()
    register_skill_tools(registry, skills)
    return registry


def test_skill_tool_registered_core_read_only(tmp_path: Path) -> None:
    spec = _reg(tmp_path).get("skill")
    assert spec is not None
    assert spec.core is True
    assert spec.risk.name == "READ_ONLY"


def test_skill_tool_returns_body(tmp_path: Path) -> None:
    result = _reg(tmp_path).dispatch("skill", {"name": "pdf-tools"})
    assert result.ok is True
    assert "Step one: open it." in result.content


def test_skill_tool_unknown_fails(tmp_path: Path) -> None:
    result = _reg(tmp_path).dispatch("skill", {"name": "ghost"})
    assert result.ok is False
    assert result.category == ErrorCategory.NOT_FOUND
