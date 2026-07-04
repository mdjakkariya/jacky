"""Tests for the code navigation tools (glob + grep)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.search import glob_files, grep


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


def _grep_tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("import os\nfoo = 1\n")
    (root / "pkg" / "b.py").write_text("bar = 2\nfoo = 3\n")
    (root / "notes.txt").write_text("nothing here\n")


def test_grep_files_with_matches_default(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "notes.txt" not in out


def test_grep_content_mode_has_file_line(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="content")
    assert "a.py:2:foo = 1" in out
    assert "b.py:2:foo = 3" in out


def test_grep_count_mode(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="count")
    assert ":1" in out  # each file has one match


def test_grep_glob_filter(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "c.md").write_text("foo in markdown\n")
    out = grep("foo", _broker(tmp_path), str(tmp_path), glob="*.py")
    assert "c.md" not in out
    assert "a.py" in out


def test_grep_ignore_case(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "d.py").write_text("FOO = 9\n")
    out = grep("foo", _broker(tmp_path), str(tmp_path), ignore_case=True, output_mode="content")
    assert "d.py:1:FOO = 9" in out


def test_grep_no_matches(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("zzz-not-here", _broker(tmp_path), str(tmp_path))
    assert "no matches" in out.lower()


def test_grep_bad_regex_does_not_raise(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("(unclosed", _broker(tmp_path), str(tmp_path))
    assert isinstance(out, str)
    assert "valid" in out.lower() or "pattern" in out.lower()


def test_grep_bad_output_mode(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path), str(tmp_path), output_mode="bogus")
    assert "output_mode" in out


def test_grep_skips_binary(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    (tmp_path / "pkg" / "blob.bin").write_bytes(b"foo\x00\x01binary")
    out = grep("foo", _broker(tmp_path), str(tmp_path))
    assert "blob.bin" not in out


def test_grep_denied_when_not_granted(tmp_path: Path) -> None:
    _grep_tree(tmp_path)
    out = grep("foo", _broker(tmp_path, grant=False), str(tmp_path))
    assert "don't have access" in out.lower()
