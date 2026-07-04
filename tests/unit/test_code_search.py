"""Tests for the code navigation tools (glob + grep)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.search import glob_files


class _FakeConfirmer:
    def __init__(self, grant: bool) -> None:
        self._grant = grant

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return self._grant

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default if self._grant else ""


def _broker(tmp_path: Path, *, grant: bool = True) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer(grant))


def _tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    (root / "pkg" / "b.py").write_text("y = 2\n")
    (root / "readme.md").write_text("# hi\n")


def test_glob_lists_matching_files(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.py", _broker(tmp_path), str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "readme.md" not in out


def test_glob_no_matches(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.rs", _broker(tmp_path), str(tmp_path))
    assert "no files" in out.lower()


def test_glob_denied_when_not_granted(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("**/*.py", _broker(tmp_path, grant=False), str(tmp_path))
    assert "don't have access" in out.lower()


def test_glob_empty_pattern(tmp_path: Path) -> None:
    out = glob_files("", _broker(tmp_path), str(tmp_path))
    assert "pattern" in out.lower()


def test_glob_bad_pattern_does_not_raise(tmp_path: Path) -> None:
    _tree(tmp_path)
    out = glob_files("/abs/pattern", _broker(tmp_path), str(tmp_path))  # non-relative → ValueError
    assert isinstance(out, str)
