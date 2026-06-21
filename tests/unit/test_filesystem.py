"""Tests for the sandboxed filesystem tools."""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import Risk
from autobot.tools.filesystem import FileTools, register_filesystem_tools
from autobot.tools.registry import ToolRegistry
from autobot.tools.sandbox import Sandbox


def _tools(tmp_path: Path) -> FileTools:
    return FileTools(Sandbox(tmp_path / "ws"))


def test_create_file_writes_content(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    msg = tools.create_file("hello.txt", "hi there")
    assert "created" in msg
    assert (tools._sandbox.root / "hello.txt").read_text() == "hi there"


def test_create_file_reports_absolute_path(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    msg = tools.create_file("sub/hello.txt", "hi")
    # The full path lets the assistant answer "where is it?" truthfully.
    assert str(tools._sandbox.root / "sub" / "hello.txt") in msg


def test_read_file_returns_content_and_path(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("notes.txt", "remember the milk")
    msg = tools.read_file("notes.txt")
    assert "remember the milk" in msg
    assert str(tools._sandbox.root / "notes.txt") in msg


def test_read_missing_file_is_reported(tmp_path: Path) -> None:
    assert "not found" in _tools(tmp_path).read_file("nope.txt")


def test_read_file_truncates_large_content(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("big.txt", "A" * 50_000)
    assert "truncated" in tools.read_file("big.txt")


def test_list_files_shows_existing_files(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("a.txt", "x")
    tools.create_file("dir/b.txt", "y")
    msg = tools.list_files()
    assert "a.txt" in msg and "dir/b.txt" in msg


def test_list_files_on_empty_workspace(tmp_path: Path) -> None:
    assert "no files" in _tools(tmp_path).list_files()


def test_list_files_confirms_after_delete(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("temp.txt", "x")
    tools.delete_file("temp.txt")
    assert "temp.txt" not in tools.list_files()


def test_move_file_renames(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("a.txt", "x")
    msg = tools.move_file("a.txt", "b.txt")
    assert "moved" in msg
    assert not (tools._sandbox.root / "a.txt").exists()
    assert (tools._sandbox.root / "b.txt").read_text() == "x"


def test_delete_file_removes_and_confirms(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("gone.txt", "x")
    msg = tools.delete_file("gone.txt")
    assert "deleted" in msg and "confirmed gone" in msg
    assert not (tools._sandbox.root / "gone.txt").exists()


def test_delete_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    assert "not found" in _tools(tmp_path).delete_file("nope.txt")


def test_registration_sets_expected_risk_levels(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_filesystem_tools(registry, Sandbox(tmp_path / "ws"))
    assert registry.get("create_file").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("read_file").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("list_files").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("move_file").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("delete_file").risk is Risk.DESTRUCTIVE  # type: ignore[union-attr]
