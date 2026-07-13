"""A fresh throwaway git repo per scenario — the harness's safety keystone.

The coder daemon jails to its launch cwd, so running each scenario in a temporary git
repo means real edits/commands/checkpoints happen here, never in the user's project. The
repo starts with the seed files committed, so ``git diff HEAD`` (what ``/diff`` and the
per-turn diff use) shows exactly the agent's changes.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from autobot.logging_setup import get_logger

_log = get_logger("e2e")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@contextmanager
def workspace(seed_files: dict[str, str], *, keep: bool = False) -> Iterator[Path]:
    """Yield a temp git repo seeded with ``seed_files`` and an initial commit."""
    root = Path(tempfile.mkdtemp(prefix="jack-e2e-"))
    ok = False
    try:
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "e2e@jack.local")
        _git(root, "config", "user.name", "jack-e2e")
        for rel, content in seed_files.items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "e2e: seed", "--allow-empty")
        _log.info("workspace ready path=%s files=%d", root, len(seed_files))
        yield root
        ok = True
    finally:
        if keep or not ok:
            _log.info("workspace preserved path=%s", root)
        else:
            shutil.rmtree(root, ignore_errors=True)
