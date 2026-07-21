"""Tests for skill sourcing: config fields, SkillHit, and SourcePin types."""

from __future__ import annotations

import json
from pathlib import Path

from autobot.config import Settings
from autobot.skills.source import SkillHit, SourcePin


def test_skill_registries_defaults_to_empty_list() -> None:
    """skill_registries config field defaults to an empty list."""
    assert Settings().skill_registries == []


def test_skill_cache_dir_has_default() -> None:
    """skill_cache_dir config field defaults to ~/.autobot/skill-cache."""
    assert Settings().skill_cache_dir == "~/.autobot/skill-cache"


def test_skill_hit_holds_all_fields() -> None:
    """SkillHit dataclass stores name, description, repo, subpath, sha."""
    hit = SkillHit(
        name="my-skill",
        description="A test skill",
        repo="github.com/user/repo",
        subpath="skills/my-skill",
        sha="abc123def456",
    )
    assert hit.name == "my-skill"
    assert hit.description == "A test skill"
    assert hit.repo == "github.com/user/repo"
    assert hit.subpath == "skills/my-skill"
    assert hit.sha == "abc123def456"


def test_source_pin_write_then_read_roundtrips(tmp_path: Path) -> None:
    """SourcePin.write() and read() round-trip correctly."""
    original = SourcePin(repo="github.com/user/repo", sha="abc123", subpath="skills/foo")
    original.write(tmp_path)

    loaded = SourcePin.read(tmp_path)
    assert loaded == original
    assert loaded.repo == "github.com/user/repo"
    assert loaded.sha == "abc123"
    assert loaded.subpath == "skills/foo"


def test_source_pin_read_returns_none_when_file_missing(tmp_path: Path) -> None:
    """SourcePin.read() returns None if .jack-source.json is absent."""
    result = SourcePin.read(tmp_path)
    assert result is None


def test_source_pin_read_returns_none_when_json_malformed(tmp_path: Path) -> None:
    """SourcePin.read() returns None if .jack-source.json is malformed."""
    source_file = tmp_path / ".jack-source.json"
    source_file.write_text("{ invalid json", encoding="utf-8")

    result = SourcePin.read(tmp_path)
    assert result is None


def test_source_pin_read_returns_none_when_missing_keys(tmp_path: Path) -> None:
    """SourcePin.read() returns None if .jack-source.json is missing required keys."""
    source_file = tmp_path / ".jack-source.json"
    # Missing 'sha' key
    source_file.write_text(json.dumps({"repo": "r", "subpath": "s"}), encoding="utf-8")

    result = SourcePin.read(tmp_path)
    assert result is None


def test_source_pin_to_json() -> None:
    """SourcePin.to_json() serializes its fields as JSON."""
    pin = SourcePin(repo="github.com/user/repo", sha="abc123", subpath="skills/foo")
    json_str = pin.to_json()

    # Verify it's valid JSON
    data = json.loads(json_str)
    assert data["repo"] == "github.com/user/repo"
    assert data["sha"] == "abc123"
    assert data["subpath"] == "skills/foo"
