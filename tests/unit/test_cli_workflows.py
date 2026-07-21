"""Tests for `jack workflows` — list/show/run, in-process (no daemon)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.cli.workflows_cmd import run


def _write_workflow(root: Path, name: str, description: str) -> None:
    """Write a minimal valid WORKFLOW.md for ``name`` under ``root/<name>/WORKFLOW.md``."""
    d = root / name
    d.mkdir(parents=True)
    (d / "WORKFLOW.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n```yaml\nsteps: []\n```\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own fake ``$HOME`` and cwd, so no real dotfiles leak in."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    return home


def test_list_prints_installed_workflow(capsys: pytest.CaptureFixture[str]) -> None:
    _write_workflow(Path.home() / ".autobot" / "workflows", "foo", "A test workflow")
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert "foo" in out
    assert "A test workflow" in out


def test_list_with_no_args_defaults_to_list(capsys: pytest.CaptureFixture[str]) -> None:
    _write_workflow(Path.home() / ".autobot" / "workflows", "foo", "A test workflow")
    assert run([]) == 0
    assert "foo" in capsys.readouterr().out


def test_list_empty_prints_message(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["list"]) == 0
    assert "No workflows found." in capsys.readouterr().out


def test_show_prints_body(capsys: pytest.CaptureFixture[str]) -> None:
    _write_workflow(Path.home() / ".autobot" / "workflows", "foo", "A test workflow")
    assert run(["show", "foo"]) == 0
    out = capsys.readouterr().out
    assert "---" in out
    assert "foo" in out


def test_show_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["show", "bogus"]) == 1
    assert "No workflow named bogus." in capsys.readouterr().err


def test_run_prints_guidance_message(capsys: pytest.CaptureFixture[str]) -> None:
    _write_workflow(Path.home() / ".autobot" / "workflows", "foo", "A test workflow")
    assert run(["run", "foo"]) == 0
    out = capsys.readouterr().out
    assert "jack workflows run" in out.lower() or "coding turn" in out.lower()


def test_run_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["run", "ghost"]) == 1
    assert "No workflow named ghost." in capsys.readouterr().err


def test_unknown_subcommand_prints_usage_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["bogus"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_help_prints_usage_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()
