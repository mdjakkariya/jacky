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


def test_live_fragments_is_a_single_status_line() -> None:
    frags = lr.live_fragments("Running command", "⠙", 4.0, width=80)
    flat = "".join(text for _style, text in frags)
    assert "⠙" in flat and "Running command" in flat and "esc to interrupt" in flat
    assert "\n" not in flat  # always a single line — no secondary preview row
