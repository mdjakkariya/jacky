"""Tests for the broker-based filesystem tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from autobot.core.types import Risk
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.filesystem import FileTools, register_filesystem_tools
from autobot.tools.registry import ToolError, ToolRegistry


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


def test_delete_missing_file_raises_tool_error(tmp_path: Path) -> None:
    # A missing target must be a FAILURE, not a success-looking string (issue #40):
    # otherwise dispatch records ok=True and the model over-claims "deleted".
    with pytest.raises(ToolError):
        _tools(tmp_path).delete_file("nope.txt")


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


# --- error / denied branches (new coverage) ----------------------------------


class _No:
    """Stub confirmer that always declines."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return False

    def choose(
        self,
        prompt: str,
        options: list[dict[str, str]],
        kind: str = "read",
        default: str = "read",
    ) -> str:
        return ""


def _denied_broker(tmp_path: Path) -> tuple[AccessBroker, AccessPolicy]:
    """A broker whose confirmer always declines; cwd is an ungranted external folder.

    The cwd is set to ``outside/`` (never granted) so any relative path resolves
    outside the workspace and the broker must ask the user — who says no (_No).
    """
    ws = tmp_path / "ws"
    pol = AccessPolicy(tmp_path / "access.json", ws)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Grant temporarily so set_cwd succeeds, then revoke so broker must ask and _No declines.
    pol.grant(outside, write=True)
    pol.set_cwd(outside)
    pol.revoke(outside)
    broker = AccessBroker(pol, _No())
    return broker, pol


def test_create_file_denied_returns_message(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    msg = tools.create_file("notes.txt", "hi")
    assert msg  # broker returns a user-friendly string, not an exception
    # The denial message from AccessBroker mentions access/grant.
    assert "access" in msg.lower() or "couldn't" in msg.lower() or "don't" in msg.lower()


def test_read_file_denied_returns_message(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    msg = tools.read_file("notes.txt")
    assert msg
    assert "access" in msg.lower() or "couldn't" in msg.lower() or "don't" in msg.lower()


def test_read_file_on_directory_returns_folder_message(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    # Create a sub-directory inside the workspace; reading it should say "folder".
    subdir = tmp_path / "ws" / "subdir"
    subdir.mkdir(parents=True)
    msg = tools.read_file("subdir")
    assert "folder" in msg


def test_list_files_on_missing_subdir(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    msg = tools.list_files("nonexistent_subdir")
    assert "not found" in msg


def test_list_files_on_a_file_returns_file_info(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.create_file("info.txt", "data")
    # Pass the file name as the subdir argument — broker resolves it to a file path.
    msg = tools.list_files("info.txt")
    assert "info.txt" in msg and "exists" in msg


def test_list_files_denied_returns_message(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    msg = tools.list_files()
    assert msg
    assert "access" in msg.lower() or "couldn't" in msg.lower() or "don't" in msg.lower()


def test_move_file_missing_source_raises_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _tools(tmp_path).move_file("ghost.txt", "dest.txt")


def test_move_file_denied_raises_tool_error(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    with pytest.raises(ToolError) as ei:
        tools.move_file("a.txt", "b.txt")
    assert "access" in str(ei.value).lower() or "don't" in str(ei.value).lower()


def test_delete_file_denied_raises_tool_error(tmp_path: Path) -> None:
    broker, _pol = _denied_broker(tmp_path)
    tools = FileTools(broker)
    with pytest.raises(ToolError) as ei:
        tools.delete_file("notes.txt")
    assert "access" in str(ei.value).lower() or "don't" in str(ei.value).lower()


def test_delete_directory_is_refused(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    subdir = tmp_path / "ws" / "keep"
    subdir.mkdir(parents=True)
    msg = tools.delete_file("keep")
    assert "refusing" in msg or "folder" in msg


def test_delete_missing_reports_failure_through_dispatch(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    reg = ToolRegistry()
    for spec in tools.specs():
        reg.register(spec)
    result = reg.dispatch("delete_file", {"path": "nope.txt"})
    assert result.ok is False  # was ok=True before the fix
    assert "deleted" not in result.content.lower()


# --- whitespace/Unicode-tolerant filename matching (issue #40) --------------


def test_delete_file_matches_narrow_no_break_space(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    ws = tmp_path / "ws"
    real = ws / ("Screenshot 9.25.25" + "\u202f" + "PM.png")  # real name (U+202F)
    real.write_text("x")
    msg = tools.delete_file("Screenshot 9.25.25 PM.png")  # model re-typed with a regular space
    assert "deleted" in msg
    assert not real.exists()


def test_read_file_matches_narrow_no_break_space(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    ws = tmp_path / "ws"
    (ws / ("Note 1.05.00" + "\u202f" + "AM.txt")).write_text("hello")
    out = tools.read_file("Note 1.05.00 AM.txt")  # regular space
    assert "hello" in out


def test_move_file_matches_narrow_no_break_space(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    ws = tmp_path / "ws"
    (ws / ("Clip 6.39.16" + "\u202f" + "PM.mov")).write_text("v")
    msg = tools.move_file("Clip 6.39.16 PM.mov", "renamed.mov")  # source w/ regular space
    assert "moved" in msg
    assert (ws / "renamed.mov").exists()


def test_delete_file_still_fails_when_truly_missing(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _tools(tmp_path).delete_file("really-not-here.txt")
