"""Tests for skill sourcing: config fields, SkillHit, SourcePin, and _ensure_repo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autobot.config import Settings
from autobot.skills.source import SkillHit, SkillSourceError, SourcePin, _ensure_repo


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


def _make_local_repo(tmp_path: Path) -> Path:
    """Build a local, offline git repo under tmp_path with a valid skill in it.

    Returns the repo's path. Used as a stand-in for a real remote so
    ``_ensure_repo`` can be exercised with real ``git`` commands, offline.
    """
    repo = tmp_path / "origin-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    skill_dir = repo / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: foo\ndescription: A test skill\n---\n\n# Foo\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_ensure_repo_clones_whitelisted_repo(tmp_path: Path) -> None:
    """_ensure_repo clones a whitelisted repo and returns (dest, sha)."""
    repo = _make_local_repo(tmp_path)
    cache_dir = tmp_path / "cache"

    dest, sha = _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])

    assert (dest / "skills" / "foo" / "SKILL.md").exists()
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_ensure_repo_second_call_updates_in_place(tmp_path: Path) -> None:
    """A second _ensure_repo call updates the existing clone rather than crashing."""
    repo = _make_local_repo(tmp_path)
    cache_dir = tmp_path / "cache"

    dest1, sha1 = _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])
    dest2, sha2 = _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])

    assert dest1 == dest2
    assert len(sha2) == 40
    assert sha1 == sha2
    assert (dest2 / "skills" / "foo" / "SKILL.md").exists()


def test_ensure_repo_rejects_repo_not_in_whitelist(tmp_path: Path) -> None:
    """_ensure_repo raises SkillSourceError for a repo not in the whitelist, and clones nothing."""
    repo = _make_local_repo(tmp_path)
    cache_dir = tmp_path / "cache"

    with pytest.raises(SkillSourceError, match="not in skill_registries whitelist"):
        _ensure_repo("https://evil.example/x", cache_dir, whitelist=[str(repo)])

    # Nothing should have been cloned for the rejected repo.
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


def test_ensure_repo_second_call_detects_updates(tmp_path: Path) -> None:
    """A second _ensure_repo call detects new commits in the origin repo."""
    repo = _make_local_repo(tmp_path)
    cache_dir = tmp_path / "cache"

    # First call
    dest1, sha1 = _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])
    assert (dest1 / "skills" / "foo" / "SKILL.md").exists()

    # Add a new commit to the origin repo
    bar_dir = repo / "skills" / "bar"
    bar_dir.mkdir(parents=True)
    (bar_dir / "SKILL.md").write_text(
        "---\nname: bar\ndescription: Another test skill\n---\n\n# Bar\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add bar skill"], cwd=repo, check=True)

    # Second call should fetch the update
    dest2, sha2 = _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])

    # Verify the dest is the same but SHA changed
    assert dest1 == dest2
    assert sha1 != sha2
    assert len(sha2) == 40
    # Verify the new file exists in the updated clone
    assert (dest2 / "skills" / "bar" / "SKILL.md").exists()


def test_ensure_repo_mkdir_oserror_raises_skill_source_error(tmp_path: Path) -> None:
    """_ensure_repo raises SkillSourceError if cache_dir parent is a regular file."""
    repo = _make_local_repo(tmp_path)

    # Create a file at the location where we'd need to create a directory
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory")

    # Try to use a cache_dir under the blocker file
    cache_dir = blocker / "cache"

    # Should raise SkillSourceError, not a bare OSError
    with pytest.raises(SkillSourceError, match="skill cache setup failed"):
        _ensure_repo(str(repo), cache_dir, whitelist=[str(repo)])
