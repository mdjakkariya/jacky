"""Client-side git snapshot + diff (real git in a tmp repo)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autobot.cli.gitdiff import diff_since, snapshot


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_snapshot_then_diff_captures_change(repo: Path) -> None:
    base = snapshot(str(repo))
    assert base
    (repo / "a.txt").write_text("one\ntwo\n")
    out = diff_since(str(repo), base)
    assert out and "+two" in out


def test_no_change_diffs_to_none(repo: Path) -> None:
    base = snapshot(str(repo))
    assert diff_since(str(repo), base) is None


def test_outside_git_repo_is_none(tmp_path: Path) -> None:
    assert snapshot(str(tmp_path)) is None
    assert diff_since(str(tmp_path), None) is None
