"""workspace creates and tears down a throwaway git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pyte")

from autobot.e2e.workspace import workspace


def test_seeds_files_and_commits() -> None:
    with workspace({"foo.py": "print('hi')\n"}) as ws:
        assert (ws / "foo.py").read_text() == "print('hi')\n"
        # a clean initial commit exists (git status porcelain is empty)
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True
        ).stdout
        assert out.strip() == ""
        saved = ws
    assert not saved.exists()  # removed on clean exit


def test_keep_preserves(tmp_path: Path) -> None:
    with workspace({"a.txt": "x"}, keep=True) as ws:
        saved = ws
    assert saved.exists()
    subprocess.run(["rm", "-rf", str(saved)], check=False)  # manual cleanup
