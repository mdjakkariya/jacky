"""Client-side git diff for the TUI: snapshot the worktree before a turn, diff after.

No daemon change needed — the coder edits the same cwd the client runs in, so ``git`` sees
its edits. Everything degrades to ``None`` outside a git repo or on any git error.
"""

from __future__ import annotations

import subprocess


def _git(cwd: str, *args: str) -> tuple[int, str]:
    """Run a git command, returning ``(returncode, stdout)``; ``(1, "")`` on any failure."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def snapshot(cwd: str) -> str | None:
    """A commit-ish capturing the current worktree (for a later ``diff_since``).

    Uses ``git stash create`` (a commit object of the working tree, without modifying it);
    falls back to ``HEAD`` when the tree is clean. ``None`` outside a git work tree.
    """
    rc, _ = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return None
    rc, out = _git(cwd, "stash", "create")
    if rc == 0 and out.strip():
        return out.strip()
    rc, out = _git(cwd, "rev-parse", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def diff_since(cwd: str, base: str | None) -> str | None:
    """Unified diff of the current worktree vs ``base``; ``None`` if empty or on error."""
    if not base:
        return None
    rc, out = _git(cwd, "diff", base)
    return out if rc == 0 and out.strip() else None


def working_diff(cwd: str) -> str | None:
    """Unified diff of tracked changes (staged + unstaged) vs ``HEAD`` in ``cwd``.

    Returns ``None`` outside a git work tree, when ``HEAD`` is unborn, or when there
    is nothing to show. Untracked (never-added) files are not included.
    """
    rc, _ = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return None
    rc, out = _git(cwd, "diff", "HEAD")
    return out if rc == 0 and out.strip() else None
