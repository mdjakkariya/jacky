"""Tests for session-file retention pruning (autobot.session_log.prune_sessions)."""

from __future__ import annotations

from pathlib import Path

from autobot.session_log import prune_sessions


def _make_sessions(dir_: Path, stamps: list[str]) -> list[Path]:
    dir_.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in stamps:
        p = dir_ / f"session-{s}.md"
        p.write_text("x")
        paths.append(p)
    return paths


def test_prune_keeps_most_recent(tmp_path: Path) -> None:
    _make_sessions(tmp_path, ["20260101-000000", "20260102-000000", "20260103-000000"])
    deleted = prune_sessions(tmp_path, keep=2)
    remaining = sorted(p.name for p in tmp_path.glob("session-*.md"))
    assert remaining == ["session-20260102-000000.md", "session-20260103-000000.md"]
    assert [p.name for p in deleted] == ["session-20260101-000000.md"]


def test_prune_noop_when_under_limit(tmp_path: Path) -> None:
    _make_sessions(tmp_path, ["20260101-000000", "20260102-000000"])
    assert prune_sessions(tmp_path, keep=5) == []
    assert len(list(tmp_path.glob("session-*.md"))) == 2


def test_prune_keep_zero_deletes_all(tmp_path: Path) -> None:
    _make_sessions(tmp_path, ["20260101-000000", "20260102-000000"])
    deleted = prune_sessions(tmp_path, keep=0)
    assert len(deleted) == 2
    assert list(tmp_path.glob("session-*.md")) == []


def test_prune_ignores_non_session_files(tmp_path: Path) -> None:
    _make_sessions(tmp_path, ["20260101-000000"])
    (tmp_path / "notes.md").write_text("keep me")
    prune_sessions(tmp_path, keep=0)
    assert (tmp_path / "notes.md").exists()


def test_prune_missing_dir_is_safe(tmp_path: Path) -> None:
    assert prune_sessions(tmp_path / "nope", keep=3) == []
