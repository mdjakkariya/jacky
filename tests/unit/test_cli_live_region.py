"""Pure spinner/action composer: action-label mapping, width-gated byline, fragments."""

from __future__ import annotations

from autobot.cli import live_region as lr


def test_action_label_maps_tools_to_present_continuous() -> None:
    assert lr.action_label("read_file") == "Reading file"
    assert lr.action_label("run_command") == "Running command"
    assert lr.action_label("edit_file") == "Editing file"
    assert lr.action_label("update_plan") == "Planning"


def test_action_label_humanizes_unknown_tools() -> None:
    assert lr.action_label("repo_stats") == "Repo stats"  # unmapped → humanized name
    assert lr.action_label("") == lr.DEFAULT_ACTION  # nothing → the default label


def test_byline_full_then_gated_by_width() -> None:
    assert "esc to interrupt" in lr.byline(12.0, width=80) and "12s" in lr.byline(12.0, 80)
    assert lr.byline(12.0, width=12) == "12s"
    assert lr.byline(12.0, width=3) == ""


def test_live_fragments_status_line_only_without_todos() -> None:
    frags = lr.live_fragments("Running command", "⠙", 4.0, [], width=80)
    flat = "".join(text for _style, text in frags)
    assert "⠙" in flat and "Running command" in flat and "esc to interrupt" in flat
    assert "\n" not in flat  # no todos → a single status line


def test_live_fragments_appends_todo_rows() -> None:
    todos = [("done", "step one"), ("in_progress", "step two")]
    frags = lr.live_fragments("Working", "⠙", 1.0, todos, width=80)
    flat = "".join(text for _style, text in frags)
    assert "Working" in flat  # spinner line first
    assert "step one" in flat and "step two" in flat  # checklist below
    assert "☑" in flat and "◐" in flat  # done + in-progress glyphs


def test_todo_panel_shows_all_when_small() -> None:
    todos = [("done", "a"), ("in_progress", "b"), ("pending", "c")]
    flat = "".join(t for _s, t in lr.todo_panel(todos, width=80))
    for step in ("a", "b", "c"):
        assert step in flat
    assert "done" not in flat  # no "N done" summary when nothing is collapsed


def test_todo_panel_windows_large_lists_around_the_focus() -> None:
    todos = [
        ("done", "d1"),
        ("done", "d2"),
        ("done", "d3"),
        ("done", "d4"),
        ("in_progress", "focus"),
        ("pending", "p1"),
        ("pending", "p2"),
        ("pending", "p3"),
        ("pending", "p4"),
        ("pending", "p5"),
    ]
    frags = lr.todo_panel(todos, width=80, max_rows=5)
    flat = "".join(t for _s, t in frags)
    rows = flat.count("\n")  # one leading "\n " per row
    assert rows <= 5  # never exceeds the row budget
    assert "focus" in flat  # the in-progress step is always visible
    assert "4 done" in flat  # completed steps before the focus collapse into a summary
    assert "more" in flat  # trailing pending steps that don't fit collapse into "+N more"
    assert "d1" not in flat and "d2" not in flat  # the collapsed done steps aren't listed
