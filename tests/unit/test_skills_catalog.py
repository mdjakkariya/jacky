"""The active-skills accessor and the prompt catalog block."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from autobot.llm.ollama_llm import skills_catalog_block
from autobot.skills.registry import SkillDir, SkillRegistry
from autobot.skills.state import active_skills, set_active_skills


@pytest.fixture(autouse=True)
def _reset_active() -> Generator[None, None, None]:
    set_active_skills(None)
    yield
    set_active_skills(None)


def test_block_empty_when_no_active_registry() -> None:
    assert active_skills() is None
    assert skills_catalog_block() == ""


def test_block_returns_catalog_when_active(tmp_path: Path) -> None:
    d = tmp_path / "pdf-tools"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: Extract PDF text.\n---\nbody", encoding="utf-8"
    )
    set_active_skills(SkillRegistry([SkillDir(tmp_path, "user", 20)]))
    block = skills_catalog_block()
    assert "pdf-tools" in block and "Extract PDF text" in block
