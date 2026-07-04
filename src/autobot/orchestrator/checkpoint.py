"""Non-destructive workspace checkpoints via a private git shadow ref.

Snapshots the full working state — tracked changes plus untracked files, honoring
``.gitignore`` — into a commit stored under a private ref (``refs/jack/checkpoints/<n>``)
*without* touching the user's real index, ``HEAD``, current branch, or working tree. This
lets the coding agent take a "before risky edit" snapshot cheaply, then later list or
restore to one, independent of whatever the user is doing with git themselves.

The git plumbing is fronted by an injectable :data:`GitRunner` seam, so the command
sequencing is unit-tested with a fake runner and needs no real git process; a companion
integration test drives the real default runner against a throwaway repo to prove the
plumbing actually works end to end.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autobot.logging_setup import get_logger

_log = get_logger("coder")

_REF_PREFIX = "refs/jack/checkpoints"

# (git args, WITHOUT the leading "git" and WITHOUT "-C <root>") + optional env overrides ->
# (returncode, combined stdout+stderr). Injectable so tests don't need a real git process;
# the root is bound into the runner (the default runner closes over it via "-C").
GitRunner = Callable[[list[str], "dict[str, str] | None"], "tuple[int, str]"]


def _make_default_runner(root: str) -> GitRunner:  # pragma: no cover - the real OS boundary
    """Build the default runner: a real ``git -C <root> <args>`` subprocess call."""

    def _run_real(args: list[str], env: dict[str, str] | None) -> tuple[int, str]:
        import subprocess

        try:
            proc = subprocess.run(
                ["git", "-C", root, *args],
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return 1, str(exc)
        combined = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode, combined

    return _run_real


def _run(
    root: str, runner: GitRunner | None, args: list[str], env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run one git command against ``root`` via ``runner`` (or the real default runner)."""
    run = runner or _make_default_runner(root)
    return run(args, env)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One workspace snapshot: its private ref, the commit sha it points at, and a label."""

    ref: str
    sha: str
    label: str


def is_git_repo(root: str, runner: GitRunner | None = None) -> bool:
    """Return whether ``root`` is inside a git working tree."""
    rc, _out = _run(root, runner, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0


def _head_sha(root: str, runner: GitRunner | None) -> str | None:
    """Return the current ``HEAD`` commit sha, or ``None`` if ``HEAD`` is unborn."""
    rc, out = _run(root, runner, ["rev-parse", "--verify", "HEAD"])
    if rc != 0:
        return None
    sha = out.strip()
    return sha or None


def _next_counter(root: str, runner: GitRunner | None) -> int:
    """Return one past the highest existing checkpoint counter (0 if none exist)."""
    rc, out = _run(root, runner, ["for-each-ref", _REF_PREFIX, "--format=%(refname)"])
    if rc != 0 or not out.strip():
        return 0
    highest = -1
    for line in out.strip().splitlines():
        ref = line.strip()
        if not ref:
            continue
        suffix = ref.rsplit("/", 1)[-1]
        try:
            highest = max(highest, int(suffix))
        except ValueError:
            continue
    return highest + 1


def create_checkpoint(root: str, label: str, runner: GitRunner | None = None) -> Checkpoint | None:
    """Snapshot the full working state (tracked + untracked, gitignore-respecting).

    Builds the snapshot entirely in a temporary index (``GIT_INDEX_FILE``), so the
    user's real index, ``HEAD``, current branch, and working tree are never touched.
    Returns ``None`` if ``root`` isn't a git repo or any plumbing step fails.
    """
    if not is_git_repo(root, runner):
        _log.warning("create_checkpoint: not a git repo root=%s", root)
        return None

    parent = _head_sha(root, runner)
    stamp = f"jack-index-{os.getpid()}-{int(time.time() * 1000)}"
    tmp_index = Path(root) / ".git" / stamp
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        if parent is not None:
            rc, out = _run(root, runner, ["read-tree", "HEAD"], env)
            if rc != 0:
                _log.warning("create_checkpoint: read-tree failed: %s", out)
                return None

        rc, out = _run(root, runner, ["add", "-A"], env)
        if rc != 0:
            _log.warning("create_checkpoint: add -A failed: %s", out)
            return None

        rc, out = _run(root, runner, ["write-tree"], env)
        if rc != 0 or not out.strip():
            _log.warning("create_checkpoint: write-tree failed: %s", out)
            return None
        tree_sha = out.strip()

        commit_args = ["commit-tree", tree_sha]
        if parent is not None:
            commit_args += ["-p", parent]
        commit_args += ["-m", label]
        rc, out = _run(root, runner, commit_args)
        if rc != 0 or not out.strip():
            _log.warning("create_checkpoint: commit-tree failed: %s", out)
            return None
        commit_sha = out.strip()

        counter = _next_counter(root, runner)
        ref = f"{_REF_PREFIX}/{counter}"
        rc, out = _run(root, runner, ["update-ref", ref, commit_sha])
        if rc != 0:
            _log.warning("create_checkpoint: update-ref failed: %s", out)
            return None
    finally:
        with contextlib.suppress(OSError):
            tmp_index.unlink()

    _log.info("checkpoint created ref=%s sha=%s label=%r", ref, commit_sha, label)
    return Checkpoint(ref=ref, sha=commit_sha, label=label)


def list_checkpoints(root: str, runner: GitRunner | None = None) -> list[Checkpoint]:
    """List all checkpoints, newest (highest counter) first. Empty list on any failure."""
    rc, out = _run(
        root, runner, ["for-each-ref", _REF_PREFIX, "--format=%(refname) %(objectname) %(subject)"]
    )
    if rc != 0 or not out.strip():
        return []

    checkpoints: list[Checkpoint] = []
    for line in out.strip().splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) < 2:
            continue
        ref, sha = parts[0], parts[1]
        label = parts[2] if len(parts) > 2 else ""
        checkpoints.append(Checkpoint(ref=ref, sha=sha, label=label))

    def _counter(cp: Checkpoint) -> int:
        try:
            return int(cp.ref.rsplit("/", 1)[-1])
        except ValueError:
            return -1

    checkpoints.sort(key=_counter, reverse=True)
    return checkpoints


def restore_checkpoint(
    root: str, ref_or_sha: str, runner: GitRunner | None = None
) -> tuple[bool, str]:
    """Restore the working tree + index to a checkpoint's snapshot.

    Known MVP limitation: this resets *tracked* paths (as of the snapshot) back to
    their snapshotted content, but files created *after* the snapshot are not removed
    — v1 does not delete extra untracked files, only reverts what the snapshot covers.
    """
    rc, out = _run(root, runner, ["rev-parse", "--verify", ref_or_sha])
    if rc != 0:
        _log.warning("restore_checkpoint: unknown ref/sha=%s: %s", ref_or_sha, out)
        return False, "no such checkpoint"
    sha = out.strip() or ref_or_sha

    rc, out = _run(root, runner, ["read-tree", sha])
    if rc != 0:
        _log.warning("restore_checkpoint: read-tree failed: %s", out)
        return False, f"failed to read snapshot tree: {out.strip()}"

    rc, out = _run(root, runner, ["checkout-index", "-a", "-f"])
    if rc != 0:
        _log.warning("restore_checkpoint: checkout-index failed: %s", out)
        return False, f"failed to write files from snapshot: {out.strip()}"

    restore_args = ["restore", f"--source={sha}", "--worktree", "--staged", "--", "."]
    rc, out = _run(root, runner, restore_args)
    if rc != 0:
        _log.warning("restore_checkpoint: restore failed: %s", out)
        return False, f"failed to restore worktree: {out.strip()}"

    _log.info("checkpoint restored sha=%s", sha)
    return True, f"restored to {ref_or_sha}"
