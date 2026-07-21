"""Tests for `jack skills` — list/search/add/remove/show, in-process (no daemon)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autobot.cli.skills_cmd import run
from autobot.config import Settings


def _write_skill(root: Path, name: str, description: str) -> None:
    """Write a minimal valid SKILL.md for ``name`` under ``root/<name>/SKILL.md``."""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _make_registry_repo(tmp_path: Path) -> Path:
    """A local git repo (stand-in for a real registry) containing one `weather` skill."""
    repo = tmp_path / "registry-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    _write_skill(repo / "skills", "weather", "Get the weather forecast for a city")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own fake ``$HOME`` and cwd, so no real dotfiles leak in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    return home


def test_list_prints_installed_skill(capsys: pytest.CaptureFixture[str]) -> None:
    _write_skill(Path.home() / ".autobot" / "skills", "foo", "A test skill")
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert "foo" in out
    assert "A test skill" in out


def test_list_with_no_args_defaults_to_list(capsys: pytest.CaptureFixture[str]) -> None:
    _write_skill(Path.home() / ".autobot" / "skills", "foo", "A test skill")
    assert run([]) == 0
    assert "foo" in capsys.readouterr().out


def test_list_empty_prints_message(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["list"]) == 0
    assert "No skills installed." in capsys.readouterr().out


def test_search_finds_registry_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry_repo(tmp_path)
    monkeypatch.setattr(
        Settings,
        "load",
        lambda: Settings(skill_registries=[str(repo)], skill_cache_dir=str(tmp_path / "cache")),
    )
    assert run(["search", "weather"]) == 0
    out = capsys.readouterr().out
    assert "weather" in out
    assert str(repo) in out


def test_search_no_registries_configured(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["search", "weather"]) == 0
    assert "No skill registries configured" in capsys.readouterr().out


def test_search_no_match_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry_repo(tmp_path)
    monkeypatch.setattr(
        Settings,
        "load",
        lambda: Settings(skill_registries=[str(repo)], skill_cache_dir=str(tmp_path / "cache")),
    )
    assert run(["search", "nonexistentxyz"]) == 0
    assert "No matching skill found." in capsys.readouterr().out


def test_add_installs_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry_repo(tmp_path)
    monkeypatch.setattr(
        Settings,
        "load",
        lambda: Settings(skill_registries=[str(repo)], skill_cache_dir=str(tmp_path / "cache")),
    )
    assert run(["add", "weather"]) == 0
    installed = Path.home() / ".autobot" / "skills" / "weather" / "SKILL.md"
    assert installed.exists()
    assert "Installed weather" in capsys.readouterr().out


def test_add_no_match_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_registry_repo(tmp_path)
    monkeypatch.setattr(
        Settings,
        "load",
        lambda: Settings(skill_registries=[str(repo)], skill_cache_dir=str(tmp_path / "cache")),
    )
    assert run(["add", "nonexistent"]) == 1
    assert capsys.readouterr().err


def test_remove_deletes_installed_skill(capsys: pytest.CaptureFixture[str]) -> None:
    home = Path.home()
    _write_skill(home / ".autobot" / "skills", "weather", "Get the weather")
    assert run(["remove", "weather"]) == 0
    assert not (home / ".autobot" / "skills" / "weather").exists()
    assert "Removed weather" in capsys.readouterr().out


def test_remove_missing_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["remove", "nope"]) == 1
    assert capsys.readouterr().err


def test_remove_twice_second_call_fails(capsys: pytest.CaptureFixture[str]) -> None:
    home = Path.home()
    _write_skill(home / ".autobot" / "skills", "weather", "Get the weather")
    assert run(["remove", "weather"]) == 0
    assert run(["remove", "weather"]) == 1


def test_show_prints_body(capsys: pytest.CaptureFixture[str]) -> None:
    _write_skill(Path.home() / ".autobot" / "skills", "foo", "A test skill")
    assert run(["show", "foo"]) == 0
    assert "# foo" in capsys.readouterr().out


def test_show_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["show", "bogus"]) == 1
    assert capsys.readouterr().err


def test_unknown_subcommand_prints_usage_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["bogus"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_help_prints_usage_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()
