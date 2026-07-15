"""Pure spinner/activity composer: deterministic verbs, width-gated byline, fragments."""

from __future__ import annotations

from autobot.cli import live_region as lr


def test_verb_is_deterministic_by_index() -> None:
    assert lr.verb_for(0) == lr.verb_for(len(lr.VERBS))
    assert lr.verb_for(0) != lr.verb_for(1)


def test_byline_full_then_gated_by_width() -> None:
    assert "esc to interrupt" in lr.byline(12.0, width=80) and "12s" in lr.byline(12.0, 80)
    assert lr.byline(12.0, width=12) == "12s"
    assert lr.byline(12.0, width=3) == ""


def test_live_fragments_spinner_line_only_when_no_activity() -> None:
    frags = lr.live_fragments("Working", "⠙", 4.0, activity="", width=80)
    flat = "".join(text for _style, text in frags)
    assert "⠙" in flat and "Working" in flat and "esc to interrupt" in flat
    assert "\n" not in flat  # one line when there's no activity


def test_live_fragments_includes_activity_line_below_spinner() -> None:
    frags = lr.live_fragments("Working", "⠙", 4.0, activity="Read parser.py", width=80)
    flat = "".join(text for _style, text in frags)
    assert "Read parser.py" in flat
    assert flat.index("Working") < flat.index("Read parser.py")  # spinner above activity
