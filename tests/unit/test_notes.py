"""Tests for the macOS Notes tools (osascript via an injected runner)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.notes import NotesTools, register_notes_tools
from autobot.tools.registry import ToolRegistry


class FakeRunner:
    """Records the argv it was called with and returns a canned (rc, output)."""

    def __init__(self, result: tuple[int, str] = (0, "")) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        return self.result


# --- note() upsert -------------------------------------------------------


def test_note_create_branch_passes_title_text_and_empty_folder() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    msg = tools.note("buy milk", "2% gallon")
    argv = runner.calls[-1]
    assert argv[0] == "osascript" and argv[1] == "-e"
    # title, text, folder("") are the three trailing data args.
    assert argv[-3:] == ["buy milk", "2% gallon", ""]
    assert "buy milk" in msg and "Created" in msg


def test_note_append_branch_reports_append() -> None:
    runner = FakeRunner((0, "appended"))
    tools = NotesTools(runner)
    msg = tools.note("shopping", "eggs")
    assert "shopping" in msg
    assert "Added" in msg or "Appended" in msg


def test_note_folder_is_passed_through() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    tools.note("standup", "ship notes tool", folder="Work")
    assert runner.calls[-1][-1] == "Work"


def test_note_blank_title_asks_instead_of_creating() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    msg = tools.note("   ", "something")
    assert "?" in msg  # it asks
    assert runner.calls == []  # and never touches osascript


def test_note_runner_failure_returns_friendly_message_no_raise() -> None:
    runner = FakeRunner((1, "boom"))
    tools = NotesTools(runner)
    msg = tools.note("groceries", "milk")
    assert "groceries" in msg
    assert "boom" in msg  # detail surfaced, no exception


# --- registration --------------------------------------------------------


def test_register_adds_note_tool_as_write() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("note").risk is Risk.WRITE  # type: ignore[union-attr]
