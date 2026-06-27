"""Tests for the broker-based filesystem tools."""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.filesystem import FileTools, register_filesystem_tools
from autobot.tools.registry import ToolRegistry


class _Yes:
    """Stub confirmer that always approves and picks 'write'."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        return "write"


def _tools(tmp_path: Path) -> FileTools:
    ws = tmp_path / "ws"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    return FileTools(AccessBroker(pol, _Yes()))


def test_create_file_writes_content(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    msg = tools.create_file("hello.txt", "hi there")
    assert "created" in msg
    assert (tmp_path / "ws" / "hello.txt").read_text() == "hi there"


def test_create_file_reports_absolute_path(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    msg = tools.create_file("sub/hello.txt", "hi")
    # The full path lets the assistant answer "where is it?" truthfully.
    assert str(tmp_path / "ws" / "sub" / "hello.txt") in msg


def test_read_file_returns_content_and_path(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("notes.txt", "remember the milk")
    msg = tools.read_file("notes.txt")
    assert "remember the milk" in msg
    assert str(tmp_path / "ws" / "notes.txt") in msg


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
    assert not (tmp_path / "ws" / "a.txt").exists()
    assert (tmp_path / "ws" / "b.txt").read_text() == "x"


def test_delete_file_removes_and_confirms(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("gone.txt", "x")
    msg = tools.delete_file("gone.txt")
    assert "deleted" in msg and "confirmed gone" in msg
    assert not (tmp_path / "ws" / "gone.txt").exists()


def test_delete_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    assert "not found" in _tools(tmp_path).delete_file("nope.txt")


def test_registration_sets_expected_risk_levels(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    broker = AccessBroker(pol, _Yes())
    registry = ToolRegistry()
    register_filesystem_tools(registry, broker)
    assert registry.get("create_file").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("read_file").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("list_files").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("move_file").risk is Risk.WRITE  # type: ignore[union-attr]
    assert registry.get("delete_file").risk is Risk.DESTRUCTIVE  # type: ignore[union-attr]


def test_create_file_lands_in_active_folder(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    proj = tmp_path / "proj"
    proj.mkdir()
    pol = AccessPolicy(tmp_path / "access.json", ws)
    pol.grant(proj, write=True)
    pol.set_cwd(proj)  # active folder is the project
    tools = FileTools(AccessBroker(pol, _Yes()))

    out = tools.create_file("demo.txt", "hi")
    assert (proj / "demo.txt").read_text() == "hi"  # landed in the active folder, not ws
    assert "demo.txt" in out


def test_create_file_defaults_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    pol = AccessPolicy(tmp_path / "access.json", ws)  # cwd defaults to ws
    tools = FileTools(AccessBroker(pol, _Yes()))
    tools.create_file("a.txt", "x")
    assert (ws / "a.txt").read_text() == "x"  # default behavior unchanged
