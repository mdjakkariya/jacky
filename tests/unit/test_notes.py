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


# --- list_notes ----------------------------------------------------------


def test_list_notes_parses_rows_and_passes_filters() -> None:
    out = "shopping\tNotes\tMonday, June 29, 2026 at 9:00:00 AM\n"
    out += "ideas\tWork\tSunday, June 1, 2026 at 1:00:00 PM\n"
    runner = FakeRunner((0, out))
    tools = NotesTools(runner)
    msg = tools.list_notes(query="shop", folder="Notes")
    # folder, query are the two trailing data args.
    assert runner.calls[-1][-2:] == ["Notes", "shop"]
    assert "shopping" in msg and "ideas" in msg


def test_list_notes_empty_reports_none() -> None:
    runner = FakeRunner((0, ""))
    tools = NotesTools(runner)
    assert "no notes" in tools.list_notes().lower()


def test_list_notes_caps_output() -> None:
    out = "".join(f"note{i}\tNotes\twhenever\n" for i in range(80))
    runner = FakeRunner((0, out))
    tools = NotesTools(runner)
    msg = tools.list_notes()
    assert "more" in msg  # the cap surfaced a "+N more" tail


# --- read_note -----------------------------------------------------------


def test_read_note_returns_plaintext() -> None:
    runner = FakeRunner((0, "shopping\nmilk\neggs"))
    tools = NotesTools(runner)
    msg = tools.read_note("shopping")
    assert runner.calls[-1][-1] == "shopping"
    assert "milk" in msg and "eggs" in msg


def test_read_note_missing_says_so() -> None:
    runner = FakeRunner((0, "NONE"))
    tools = NotesTools(runner)
    assert "shopping" in tools.read_note("shopping").lower()


# --- list_folders --------------------------------------------------------


def test_list_folders_parses_names() -> None:
    runner = FakeRunner((0, "Notes\nWork\nRecipes\n"))
    tools = NotesTools(runner)
    msg = tools.list_folders()
    assert "Work" in msg and "Recipes" in msg


def test_read_tools_register_as_read_only() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("list_notes").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("read_note").risk is Risk.READ_ONLY  # type: ignore[union-attr]
    assert registry.get("list_folders").risk is Risk.READ_ONLY  # type: ignore[union-attr]
