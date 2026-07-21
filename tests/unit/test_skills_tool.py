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


def test_skill_tool_result_includes_read_skill_file_hint(tmp_path: Path) -> None:
    result = _reg(tmp_path).dispatch("skill", {"name": "pdf-tools"})
    assert result.ok is True
    assert "read_skill_file" in result.content
    assert 'name="pdf-tools"' in result.content


def test_read_skill_file_tool_registered_core_read_only(tmp_path: Path) -> None:
    spec = _reg(tmp_path).get("read_skill_file")
    assert spec is not None
    assert spec.core is True
    assert spec.risk.name == "READ_ONLY"


def test_read_skill_file_returns_reference_content(tmp_path: Path) -> None:
    registry = _reg(tmp_path)
    refs = tmp_path / "pdf-tools" / "references"
    refs.mkdir()
    (refs / "DOC.md").write_text("Reference doc content.", encoding="utf-8")
    result = registry.dispatch(
        "read_skill_file", {"name": "pdf-tools", "path": "references/DOC.md"}
    )
    assert result.ok is True
    assert result.content == "Reference doc content."


def test_read_skill_file_unknown_skill_fails(tmp_path: Path) -> None:
    result = _reg(tmp_path).dispatch(
        "read_skill_file", {"name": "ghost", "path": "references/DOC.md"}
    )
    assert result.ok is False
    assert result.category == ErrorCategory.NOT_FOUND


def test_read_skill_file_missing_file_fails(tmp_path: Path) -> None:
    result = _reg(tmp_path).dispatch(
        "read_skill_file", {"name": "pdf-tools", "path": "references/NOPE.md"}
    )
    assert result.ok is False
    assert result.category == ErrorCategory.NOT_FOUND


def test_read_skill_file_blocks_relative_traversal(tmp_path: Path) -> None:
    """Critical security check: '..' must not escape the skill's own directory."""
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the skill dir", encoding="utf-8")
    result = _reg(tmp_path).dispatch(
        "read_skill_file", {"name": "pdf-tools", "path": "../secret.txt"}
    )
    assert result.ok is False
    assert result.category == ErrorCategory.DENIED
    assert "secret" not in result.content
    assert "outside the skill dir" not in result.content


def test_read_skill_file_blocks_absolute_path_escape(tmp_path: Path) -> None:
    """Critical security check: an absolute path escaping the skill dir is refused."""
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the skill dir", encoding="utf-8")
    result = _reg(tmp_path).dispatch("read_skill_file", {"name": "pdf-tools", "path": str(secret)})
    assert result.ok is False
    assert result.category == ErrorCategory.DENIED
    assert "outside the skill dir" not in result.content
