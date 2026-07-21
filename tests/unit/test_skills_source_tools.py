"""Tests for the model-facing find_skill / install_skill tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

from autobot.core.types import ErrorCategory, Risk
from autobot.skills.source import SkillSource
from autobot.skills.source_tools import register_source_tools
from autobot.tools.registry import ToolRegistry


def _make_weather_repo(tmp_path: Path) -> Path:
    """Build a local, offline git repo under tmp_path with a "weather" skill in it.

    Returns the repo's path. Used as a stand-in for a real remote registry.
    """
    repo = tmp_path / "origin-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    weather_dir = repo / "skills" / "weather"
    weather_dir.mkdir(parents=True)
    (weather_dir / "SKILL.md").write_text(
        "---\nname: weather\ndescription: Get the weather forecast for a city\n---\n\n# Weather\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _build(tmp_path: Path) -> tuple[ToolRegistry, Path]:
    """Build a registry with source_tools registered against a real local repo fixture."""
    repo = _make_weather_repo(tmp_path)
    source = SkillSource([str(repo)], tmp_path / "cache")
    dest_root = tmp_path / "installed"
    registry = ToolRegistry()
    register_source_tools(registry, source, dest_root)
    return registry, dest_root


def test_find_skill_is_core_read_only_network(tmp_path: Path) -> None:
    """find_skill is core, READ_ONLY, and network=True."""
    registry, _ = _build(tmp_path)
    spec = registry.get("find_skill")
    assert spec is not None
    assert spec.network is True
    assert spec.core is True
    assert spec.risk == Risk.READ_ONLY


def test_install_skill_is_write_network_and_not_core(tmp_path: Path) -> None:
    """install_skill is WRITE, network=True, and not core (gated)."""
    registry, _ = _build(tmp_path)
    spec = registry.get("install_skill")
    assert spec is not None
    assert spec.network is True
    assert spec.risk == Risk.WRITE
    assert spec.core is False


def test_find_skill_returns_matching_hit(tmp_path: Path) -> None:
    """find_skill returns candidates mentioning the matching skill."""
    registry, _ = _build(tmp_path)
    result = registry.dispatch("find_skill", {"query": "weather"})
    assert result.ok is True
    assert "weather" in result.content


def test_find_skill_no_match_is_normal_ok_result(tmp_path: Path) -> None:
    """An empty search result is a normal outcome, not a ToolFailure."""
    registry, _ = _build(tmp_path)
    result = registry.dispatch("find_skill", {"query": "zzzznomatch"})
    assert result.ok is True
    assert "no matching skill" in result.content.lower()


def test_install_skill_installs_exact_match(tmp_path: Path) -> None:
    """install_skill installs the exact-name match into dest_root."""
    registry, dest_root = _build(tmp_path)
    result = registry.dispatch("install_skill", {"name": "weather"})
    assert result.ok is True
    assert (dest_root / "weather" / "SKILL.md").exists()


def test_install_skill_unknown_name_is_not_found(tmp_path: Path) -> None:
    """install_skill on an unknown name fails with NOT_FOUND, not a crash."""
    registry, _ = _build(tmp_path)
    result = registry.dispatch("install_skill", {"name": "ghost"})
    assert result.ok is False
    assert result.category == ErrorCategory.NOT_FOUND
