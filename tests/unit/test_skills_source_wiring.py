"""The skill-sourcing wiring block used by app.build(): SkillSource + source tools."""

from __future__ import annotations

from pathlib import Path

from autobot.skills.source import SkillSource
from autobot.skills.source_tools import register_source_tools
from autobot.tools.registry import ToolRegistry


def _wire(tmp_path: Path) -> ToolRegistry:
    """Mirror of app.build()'s skill-sourcing block (skills_enabled == True)."""
    source = SkillSource([], tmp_path / "cache")
    registry = ToolRegistry()
    register_source_tools(registry, source, tmp_path / "skills")
    return registry


def test_wiring_registers_find_and_install_skill_tools(tmp_path: Path) -> None:
    registry = _wire(tmp_path)

    find_skill = registry.get("find_skill")
    install_skill = registry.get("install_skill")

    assert find_skill is not None
    assert install_skill is not None
    assert find_skill.network is True
    assert install_skill.network is True
