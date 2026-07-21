"""The skills wiring block used by app.build(): registry + tool + active pointer."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from autobot.llm.ollama_llm import skills_catalog_block
from autobot.skills.registry import SkillRegistry, default_skill_dirs
from autobot.skills.state import active_skills, set_active_skills
from autobot.skills.tool import register_skill_tools
from autobot.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_active() -> Generator[None, None, None]:
    set_active_skills(None)
    yield
    set_active_skills(None)


def _wire(home: Path, project: Path) -> ToolRegistry:
    """Mirror of app.build()'s skills block (skills_enabled == True)."""
    skills = SkillRegistry(default_skill_dirs(home, project))
    set_active_skills(skills)
    registry = ToolRegistry()
    register_skill_tools(registry, skills)
    return registry


def test_wiring_registers_tool_and_active_registry(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    skill = project / ".jack" / "skills" / "greet"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: greet\ndescription: Greet the user warmly.\n---\nSay hi.", encoding="utf-8"
    )
    registry = _wire(home, project)
    assert registry.get("skill") is not None
    assert active_skills() is not None
    assert "greet" in skills_catalog_block()
