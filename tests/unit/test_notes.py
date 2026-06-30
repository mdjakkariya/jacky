"""Tests for the macOS Notes tools (osascript via an injected runner)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.notes import NotesTools, _render_html, register_notes_tools
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


def test_note_create_branch_passes_name_bodies_folder_and_mode() -> None:
    runner = FakeRunner((0, "created"))
    tools = NotesTools(runner)
    msg = tools.note("buy milk", "2% gallon")
    argv = runner.calls[-1]
    assert argv[0] == "osascript" and argv[1] == "-e"
    # data args: name (raw), create-body (HTML), append-fragment, folder, mode.
    name, create_body, append_fragment, folder, mode = argv[-5:]
    assert name == "buy milk"
    assert folder == ""
    assert mode == "append"  # default
    assert "<b>buy milk</b>" in create_body  # title becomes the first (name) line
    assert "<div>2% gallon</div>" in create_body
    assert append_fragment == "<div>2% gallon</div>"
    assert "buy milk" in msg and "Created" in msg


def test_note_replace_mode_is_passed_through() -> None:
    runner = FakeRunner((0, "replaced"))
    tools = NotesTools(runner)
    tools.note("buy milk", "fresh content", mode="replace")
    assert runner.calls[-1][-1] == "replace"


def test_note_replace_branch_reports_rewrite() -> None:
    runner = FakeRunner((0, "replaced"))
    tools = NotesTools(runner)
    msg = tools.note("RCA", "clean content", mode="replace")
    assert "RCA" in msg
    assert "Rewrote" in msg or "Replaced" in msg


def test_note_unknown_mode_defaults_to_append() -> None:
    runner = FakeRunner((0, "appended"))
    tools = NotesTools(runner)
    tools.note("x", "y", mode="bogus")
    assert runner.calls[-1][-1] == "append"


# --- _render_html (markdown-lite -> Notes HTML) --------------------------


def test_render_html_headings() -> None:
    assert _render_html("# Title") == "<h1>Title</h1>"
    assert _render_html("## Overview") == "<h2>Overview</h2>"
    assert _render_html("### Step") == "<h3>Step</h3>"


def test_render_html_bullets_grouped_into_list() -> None:
    assert _render_html("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"
    assert _render_html("* a") == "<ul><li>a</li></ul>"


def test_render_html_bold_inline() -> None:
    assert _render_html("**Email**: x") == "<div><b>Email</b>: x</div>"


def test_render_html_escapes_special_chars() -> None:
    assert _render_html("a < b & c") == "<div>a &lt; b &amp; c</div>"


def test_render_html_preserves_line_breaks_as_divs() -> None:
    assert _render_html("line1\nline2") == "<div>line1</div><div>line2</div>"


def test_render_html_blank_line_closes_list() -> None:
    assert _render_html("- a\n\ntext") == "<ul><li>a</li></ul><div>text</div>"


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
    # folder is the second-to-last data arg (mode is last).
    assert runner.calls[-1][-2] == "Work"


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


# --- move_note -----------------------------------------------------------


def test_move_note_existing_folder() -> None:
    runner = FakeRunner((0, "OK\tno"))  # createdFolder = "no"
    tools = NotesTools(runner)
    msg = tools.move_note("pasta recipe", "Recipes")
    assert runner.calls[-1][-2:] == ["pasta recipe", "Recipes"]
    assert "Recipes" in msg
    assert "new folder" not in msg.lower()


def test_move_note_creates_folder_is_announced() -> None:
    runner = FakeRunner((0, "OK\tyes"))  # createdFolder = "yes"
    tools = NotesTools(runner)
    msg = tools.move_note("pasta recipe", "Recipes")
    assert "new folder" in msg.lower() and "Recipes" in msg


def test_move_note_missing_note_says_so() -> None:
    runner = FakeRunner((0, "NONE"))
    tools = NotesTools(runner)
    assert "pasta recipe" in tools.move_note("pasta recipe", "Recipes").lower()


def test_move_note_registers_as_write() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("move_note").risk is Risk.WRITE  # type: ignore[union-attr]


# --- delete_note ---------------------------------------------------------


def test_delete_note_reports_count_and_titles() -> None:
    runner = FakeRunner((0, "2\tPasta recipe\nCake recipe\n"))
    tools = NotesTools(runner)
    msg = tools.delete_note("recipe")
    assert runner.calls[-1][-1] == "recipe"
    assert "Pasta recipe" in msg and "Cake recipe" in msg
    assert "2" in msg


def test_delete_note_no_match_says_so() -> None:
    runner = FakeRunner((0, "0\t"))
    tools = NotesTools(runner)
    assert "recipe" in tools.delete_note("recipe").lower()


def test_delete_note_blank_query_asks() -> None:
    runner = FakeRunner((0, "0\t"))
    tools = NotesTools(runner)
    msg = tools.delete_note("   ")
    assert "?" in msg
    assert runner.calls == []


def test_delete_note_registers_as_destructive() -> None:
    registry = ToolRegistry()
    register_notes_tools(registry, FakeRunner())
    assert registry.get("delete_note").risk is Risk.DESTRUCTIVE  # type: ignore[union-attr]
