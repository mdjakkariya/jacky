"""Tests for the access-gated file I/O tools (read / copy / write / edit)."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.fileio import (
    copy_file_to_clipboard,
    edit_file,
    read_file_text,
    register_file_io_tools,
    write_file,
)
from autobot.tools.registry import ToolRegistry


class _FakeConfirmer:
    """A confirmer that approves (grant) or declines everything."""

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


def test_read_file_text_reads_granted_file(tmp_path: Path) -> None:
    f = tmp_path / "proj" / "a.txt"
    f.parent.mkdir()
    f.write_text("hello there")
    out = read_file_text(str(f), _broker(tmp_path))
    assert "hello there" in out and "a.txt" in out


def test_read_file_text_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "proj" / "a.txt"
    f.parent.mkdir()
    f.write_text("secret-ish")
    out = read_file_text(str(f), _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower() and "settings" in out.lower()


def test_read_file_text_rejects_binary(tmp_path: Path) -> None:
    f = tmp_path / "b.bin"
    f.write_bytes(b"\x00\x01\x02data")
    out = read_file_text(str(f), _broker(tmp_path))
    assert "binary" in out.lower()


def test_copy_file_to_clipboard_uses_clipboard_runner(tmp_path: Path) -> None:
    f = tmp_path / "c.txt"
    f.write_text("copy this content")
    seen: list[str | None] = []

    def clip(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
        seen.append(stdin)
        return 0, ""

    out = copy_file_to_clipboard(str(f), _broker(tmp_path), clip_runner=clip)
    assert seen == ["copy this content"]
    assert "Copied 17 characters from c.txt" in out


def test_write_file_creates_file(tmp_path: Path) -> None:
    f = tmp_path / "proj" / "new.txt"
    f.parent.mkdir()
    out = write_file(str(f), "fresh content", _broker(tmp_path))
    assert f.read_text() == "fresh content"
    assert "Wrote 13 characters to new.txt" in out


def test_write_file_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # write_file makes parent folders within the grant, so create_file isn't needed.
    f = tmp_path / "proj" / "sub" / "deep.txt"
    out = write_file(str(f), "hi", _broker(tmp_path))
    assert f.read_text() == "hi"
    assert "Wrote 2 characters" in out


def test_write_file_denied_when_not_granted(tmp_path: Path) -> None:
    f = tmp_path / "proj" / "new.txt"
    f.parent.mkdir()
    out = write_file(str(f), "x", _broker(tmp_path, grant=False))
    assert "don't have access" in out.lower()
    assert not f.exists()


def test_edit_file_replaces_text(tmp_path: Path) -> None:
    f = tmp_path / "e.txt"
    f.write_text("foo and foo again")
    out = edit_file(str(f), "foo", "bar", _broker(tmp_path))
    assert f.read_text() == "bar and bar again"
    assert "2 replacements" in out


def test_edit_file_missing_text_changes_nothing(tmp_path: Path) -> None:
    f = tmp_path / "e.txt"
    f.write_text("unchanged")
    out = edit_file(str(f), "absent", "x", _broker(tmp_path))
    assert f.read_text() == "unchanged"
    assert "couldn't find that text" in out.lower()


def _registry_with_fileio(tmp_path: Path) -> ToolRegistry:
    """Build a ToolRegistry with the file-I/O tools registered."""
    reg = ToolRegistry()
    register_file_io_tools(reg, _broker(tmp_path))
    return reg


def test_write_file_handler_without_content_returns_message(tmp_path: Path) -> None:
    """Calling the registered write_file handler with only path must NOT raise.

    Before the lambda-default fix this raised TypeError; the handler must now
    return a clear, actionable message so the model knows what to add.
    """
    reg = _registry_with_fileio(tmp_path)
    spec = reg.get("write_file")
    assert spec is not None
    out = spec.handler(path="notes.txt")  # no content arg
    assert isinstance(out, str)
    assert "content" in out.lower() or "text to write" in out.lower()


def test_write_file_handler_with_content_writes(tmp_path: Path) -> None:
    """Calling the registered write_file handler with path + content writes the file."""
    target = tmp_path / "notes.txt"
    reg = _registry_with_fileio(tmp_path)
    spec = reg.get("write_file")
    assert spec is not None
    out = spec.handler(path=str(target), content="hello world")
    assert isinstance(out, str)
    assert "wrote" in out.lower()
    assert target.read_text() == "hello world"
