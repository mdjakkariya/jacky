"""Tests for git shadow-ref workspace checkpoints.

Pure logic (command sequencing) is covered with a fake :data:`GitRunner` so these
run with no real git process. One integration test drives the real default runner
against a throwaway git repo to prove the plumbing actually works end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from autobot.orchestrator.checkpoint import (
    Checkpoint,
    create_checkpoint,
    is_git_repo,
    list_checkpoints,
    restore_checkpoint,
)

# ---------------------------------------------------------------------------
# Fake runner infrastructure (pure unit tests, no real git process)
# ---------------------------------------------------------------------------


class FakeGit:
    """Records every invocation and returns scripted (returncode, output) pairs.

    ``responses`` maps a command-args tuple to its scripted result; unmatched
    commands default to ``(0, "")`` so tests only need to script what they check.
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, args: list[str], env: dict[str, str] | None) -> tuple[int, str]:
        self.calls.append((list(args), dict(env) if env else None))
        return self.responses.get(tuple(args), (0, ""))


def test_is_git_repo_true_when_rev_parse_succeeds() -> None:
    fake = FakeGit({("rev-parse", "--is-inside-work-tree"): (0, "true\n")})
    assert is_git_repo("/repo", fake) is True
    assert fake.calls == [(["rev-parse", "--is-inside-work-tree"], None)]


def test_is_git_repo_false_when_rev_parse_fails() -> None:
    fake = FakeGit({("rev-parse", "--is-inside-work-tree"): (128, "fatal: not a git repository")})
    assert is_git_repo("/repo", fake) is False


def test_create_checkpoint_returns_none_when_not_a_repo() -> None:
    fake = FakeGit({("rev-parse", "--is-inside-work-tree"): (128, "fatal: not a git repository")})
    assert create_checkpoint("/repo", "label", fake) is None
    # Should short-circuit: only the is_git_repo probe, no write-tree/commit-tree work.
    assert fake.calls == [(["rev-parse", "--is-inside-work-tree"], None)]


def test_create_checkpoint_sequence_with_existing_head() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("rev-parse", "--verify", "HEAD"): (0, "deadbeef\n"),
            ("for-each-ref", "refs/jack/checkpoints", "--format=%(refname)"): (0, ""),
            ("write-tree",): (0, "treesha123\n"),
            ("commit-tree", "treesha123", "-p", "deadbeef", "-m", "my label"): (
                0,
                "commitsha456\n",
            ),
            ("update-ref", "refs/jack/checkpoints/0", "commitsha456"): (0, ""),
        }
    )

    checkpoint = create_checkpoint("/repo", "my label", fake)

    assert checkpoint == Checkpoint(
        ref="refs/jack/checkpoints/0", sha="commitsha456", label="my label"
    )

    commands = [tuple(c[0]) for c in fake.calls]
    assert ("rev-parse", "--is-inside-work-tree") in commands
    assert ("rev-parse", "--verify", "HEAD") in commands
    assert any(c[:2] == ("read-tree", "HEAD") for c in commands)
    assert any(c[0] == "add" for c in commands)
    assert ("write-tree",) in commands
    assert ("commit-tree", "treesha123", "-p", "deadbeef", "-m", "my label") in commands
    assert ("update-ref", "refs/jack/checkpoints/0", "commitsha456") in commands

    # read-tree, add, and write-tree must all use the same temp GIT_INDEX_FILE env,
    # and it must differ from the repo's real index.
    index_envs = {
        c[1]["GIT_INDEX_FILE"]
        for c in fake.calls
        if c[1] and "GIT_INDEX_FILE" in c[1] and c[0][0] in ("read-tree", "add", "write-tree")
    }
    assert len(index_envs) == 1
    (tmp_index,) = index_envs
    assert "jack-index" in tmp_index


def test_create_checkpoint_skips_read_tree_when_head_unborn() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("rev-parse", "--verify", "HEAD"): (128, "fatal: bad revision 'HEAD'"),
            ("for-each-ref", "refs/jack/checkpoints", "--format=%(refname)"): (0, ""),
            ("write-tree",): (0, "treesha000\n"),
            ("commit-tree", "treesha000", "-m", "first"): (0, "commitsha000\n"),
            ("update-ref", "refs/jack/checkpoints/0", "commitsha000"): (0, ""),
        }
    )

    checkpoint = create_checkpoint("/repo", "first", fake)

    assert checkpoint == Checkpoint(
        ref="refs/jack/checkpoints/0", sha="commitsha000", label="first"
    )
    commands = [tuple(c[0]) for c in fake.calls]
    assert not any(c[0] == "read-tree" for c in commands)
    # No -p HEAD parent when HEAD is unborn.
    assert ("commit-tree", "treesha000", "-m", "first") in commands


def test_create_checkpoint_counter_increments_from_existing_refs() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("rev-parse", "--verify", "HEAD"): (0, "deadbeef\n"),
            ("for-each-ref", "refs/jack/checkpoints", "--format=%(refname)"): (
                0,
                "refs/jack/checkpoints/0\nrefs/jack/checkpoints/1\nrefs/jack/checkpoints/3\n",
            ),
            ("write-tree",): (0, "tree999\n"),
            ("commit-tree", "tree999", "-p", "deadbeef", "-m", "next"): (0, "commit999\n"),
            ("update-ref", "refs/jack/checkpoints/4", "commit999"): (0, ""),
        }
    )

    checkpoint = create_checkpoint("/repo", "next", fake)

    assert checkpoint is not None
    assert checkpoint.ref == "refs/jack/checkpoints/4"
    commands = [tuple(c[0]) for c in fake.calls]
    assert ("update-ref", "refs/jack/checkpoints/4", "commit999") in commands


def test_create_checkpoint_returns_none_on_write_tree_failure() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("rev-parse", "--verify", "HEAD"): (0, "deadbeef\n"),
            ("for-each-ref", "refs/jack/checkpoints", "--format=%(refname)"): (0, ""),
            ("write-tree",): (128, "fatal: something went wrong"),
        }
    )

    assert create_checkpoint("/repo", "label", fake) is None


def test_list_checkpoints_parses_and_orders_newest_first() -> None:
    fake = FakeGit(
        {
            (
                "for-each-ref",
                "refs/jack/checkpoints",
                "--format=%(refname) %(objectname) %(subject)",
            ): (
                0,
                "refs/jack/checkpoints/0 sha0 first label\n"
                "refs/jack/checkpoints/2 sha2 third label\n"
                "refs/jack/checkpoints/1 sha1 second label\n",
            )
        }
    )

    checkpoints = list_checkpoints("/repo", fake)

    assert checkpoints == [
        Checkpoint(ref="refs/jack/checkpoints/2", sha="sha2", label="third label"),
        Checkpoint(ref="refs/jack/checkpoints/1", sha="sha1", label="second label"),
        Checkpoint(ref="refs/jack/checkpoints/0", sha="sha0", label="first label"),
    ]


def test_list_checkpoints_empty_when_no_refs() -> None:
    fake = FakeGit(
        {
            (
                "for-each-ref",
                "refs/jack/checkpoints",
                "--format=%(refname) %(objectname) %(subject)",
            ): (0, "")
        }
    )
    assert list_checkpoints("/repo", fake) == []


def test_list_checkpoints_returns_empty_on_git_failure() -> None:
    fake = FakeGit(
        {
            (
                "for-each-ref",
                "refs/jack/checkpoints",
                "--format=%(refname) %(objectname) %(subject)",
            ): (128, "fatal: error")
        }
    )
    assert list_checkpoints("/repo", fake) == []


def test_restore_checkpoint_success_sequence() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--verify", "abc123"): (0, "abc123\n"),
            ("read-tree", "abc123"): (0, ""),
            ("checkout-index", "-a", "-f"): (0, ""),
            ("restore", "--source=abc123", "--worktree", "--staged", "--", "."): (0, ""),
        }
    )

    ok, msg = restore_checkpoint("/repo", "abc123", fake)

    assert ok is True
    assert "abc123" in msg
    commands = [tuple(c[0]) for c in fake.calls]
    assert ("read-tree", "abc123") in commands
    assert ("checkout-index", "-a", "-f") in commands
    assert ("restore", "--source=abc123", "--worktree", "--staged", "--", ".") in commands
    assert ("clean", "-fd") in commands  # remove files created after the snapshot


def test_restore_checkpoint_reports_clean_failure() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--verify", "abc123"): (0, "abc123\n"),
            ("clean", "-fd"): (1, "fatal: clean blocked"),
        }
    )

    ok, msg = restore_checkpoint("/repo", "abc123", fake)

    assert ok is False
    assert msg


def test_restore_checkpoint_unknown_ref_fails_fast() -> None:
    fake = FakeGit({("rev-parse", "--verify", "nope"): (128, "fatal: bad revision 'nope'")})

    ok, msg = restore_checkpoint("/repo", "nope", fake)

    assert ok is False
    assert "no such checkpoint" in msg.lower()
    # Should short-circuit: no destructive restore commands attempted.
    commands = [tuple(c[0]) for c in fake.calls]
    assert not any(c[0] in ("read-tree", "checkout-index", "restore") for c in commands)


def test_restore_checkpoint_reports_failure_reason() -> None:
    fake = FakeGit(
        {
            ("rev-parse", "--verify", "abc123"): (0, "abc123\n"),
            ("read-tree", "abc123"): (1, "fatal: could not read tree"),
        }
    )

    ok, msg = restore_checkpoint("/repo", "abc123", fake)

    assert ok is False
    assert msg


# ---------------------------------------------------------------------------
# Real-git integration test (the only "real" check; no fake runner involved)
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def test_checkpoint_round_trip_with_real_git(tmp_path: Path) -> None:
    """Real end-to-end proof: checkpoint + restore against a real git repo.

    Creates a repo, commits a.txt, edits it and adds an untracked b.txt, snapshots
    that state with create_checkpoint (real default runner), makes further edits,
    then restores and asserts a.txt is back to the checkpointed content.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")

    a_path = root / "a.txt"
    a_path.write_text("version 1\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-m", "initial")

    assert is_git_repo(str(root)) is True

    # Dirty the working tree: edit a tracked file, add an untracked one.
    a_path.write_text("version 2 (checkpointed)\n")
    b_path = root / "b.txt"
    b_path.write_text("untracked new file\n")

    checkpoint = create_checkpoint(str(root), "before risky edit")
    assert checkpoint is not None
    assert checkpoint.ref == "refs/jack/checkpoints/0"
    assert checkpoint.sha

    # The checkpoint must not have touched the user's real index/HEAD/branch/worktree.
    status_after_checkpoint = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "a.txt" in status_after_checkpoint  # still shows as modified (untouched index)
    assert "b.txt" in status_after_checkpoint  # still untracked
    assert a_path.read_text() == "version 2 (checkpointed)\n"  # worktree untouched
    branch = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "main" or branch == "master"

    listed = list_checkpoints(str(root))
    assert listed == [checkpoint]

    # Now make further edits that should be undone by restore.
    a_path.write_text("version 3 (post-checkpoint edit, should be reverted)\n")

    ok, msg = restore_checkpoint(str(root), checkpoint.sha)

    assert ok is True, msg
    assert a_path.read_text() == "version 2 (checkpointed)\n"


def test_restore_removes_post_snapshot_files_with_real_git(tmp_path: Path) -> None:
    """Restore removes files created after the snapshot, keeping snapshot + ignored files."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")

    (root / "a.txt").write_text("v1\n")
    (root / ".gitignore").write_text("*.log\n")
    _git(root, "add", "a.txt", ".gitignore")
    _git(root, "commit", "-m", "initial")

    # Untracked file present at snapshot time — must survive the restore.
    (root / "keep.txt").write_text("in the snapshot\n")

    checkpoint = create_checkpoint(str(root), "snap")
    assert checkpoint is not None

    # After the snapshot: a new (non-ignored) file, an ignored file, and a tracked edit.
    (root / "extra.txt").write_text("created after the snapshot\n")
    (root / "debug.log").write_text("ignored build artifact\n")
    (root / "a.txt").write_text("v2\n")

    ok, msg = restore_checkpoint(str(root), checkpoint.sha)
    assert ok is True, msg

    assert (root / "a.txt").read_text() == "v1\n"  # tracked change reverted
    assert (root / "keep.txt").exists()  # snapshot's untracked file preserved
    assert not (root / "extra.txt").exists()  # post-snapshot file removed (the G6 fix)
    assert (root / "debug.log").exists()  # ignored file left untouched (no -x)
