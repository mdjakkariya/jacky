"""Tests for the code-editing tools (read/write/edit/multi_edit)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.tools import read_file, write_file


class _FakeConfirmer:
    """Approves or declines every grant prompt."""

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


def test_read_file_numbers_lines(tmp_path: Path) -> None:
    f = tmp_path / "p" / "a.py"
    f.parent.mkdir()
    f.write_text("first\nsecond\nthird\n")
    out = read_file(str(f), _broker(tmp_path))
    assert "1\tfirst" in out
    assert "2\tsecond" in out
    assert "3\tthird" in out


def test_read_file_offset_and_limit(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")
    out = read_file(str(f), _broker(tmp_path), offset=3, limit=2)
    assert "3\tl3" in out and "4\tl4" in out
    assert "l2" not in out and "l5" not in out


def test_read_file_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "p" / "a.py"
    f.parent.mkdir()
    f.write_text("secret-ish")
    out = read_file(str(f), _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower()


def test_read_file_rejects_binary(tmp_path: Path) -> None:
    f = tmp_path / "b.bin"
    f.write_bytes(b"\x00\x01\x02data")
    assert "binary" in read_file(str(f), _broker(tmp_path)).lower()


def test_read_file_missing(tmp_path: Path) -> None:
    out = read_file(str(tmp_path / "nope.py"), _broker(tmp_path))
    assert "no file" in out.lower()


def test_write_file_creates_new(tmp_path: Path) -> None:
    f = tmp_path / "p" / "new.py"
    f.parent.mkdir()
    out = write_file(str(f), "print('hi')\n", _broker(tmp_path))
    assert f.read_text() == "print('hi')\n"
    assert "wrote" in out.lower()


def test_write_file_refuses_to_overwrite(tmp_path: Path) -> None:
    f = tmp_path / "exists.py"
    f.write_text("original\n")
    out = write_file(str(f), "clobber\n", _broker(tmp_path))
    assert f.read_text() == "original\n"  # untouched
    assert "already exists" in out.lower()
    assert "edit_file" in out


def test_write_file_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "p" / "new.py"
    f.parent.mkdir()
    out = write_file(str(f), "x", _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower()
    assert not f.exists()
