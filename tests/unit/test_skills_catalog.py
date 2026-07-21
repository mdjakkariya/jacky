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


def test_openai_assemble_includes_skills_block(tmp_path: Path) -> None:
    """All three providers append `skills_catalog_block()` verbatim as a system message.

    A full per-provider `_assemble` assertion requires constructing a provider with an
    injected fake client + `Session`; that path is exercised by the PTY e2e suite. This
    pins the shared content each provider injects.
    """
    d = tmp_path / "notes-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: notes-skill\ndescription: Take structured notes.\n---\nbody", encoding="utf-8"
    )
    set_active_skills(SkillRegistry([SkillDir(tmp_path, "user", 20)]))
    block = skills_catalog_block()
    assert "notes-skill" in block
