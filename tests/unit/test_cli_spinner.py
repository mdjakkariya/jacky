"""Spinner: deterministic verbs, width-gated byline, and a self-stopping live thread."""

from __future__ import annotations

import threading
from io import StringIO

from rich.console import Console

from autobot.cli import spinner


def test_verb_is_deterministic_by_index() -> None:
    assert spinner.verb_for(0) == spinner.verb_for(len(spinner.VERBS))
    assert spinner.verb_for(0) != spinner.verb_for(1)


def test_byline_full_then_gated_by_width() -> None:
    full = spinner.byline(12.0, width=80)
    assert "esc to interrupt" in full and "12s" in full
    narrow = spinner.byline(12.0, width=12)
    assert "12s" in narrow and "esc to interrupt" not in narrow
    tiny = spinner.byline(12.0, width=3)
    assert tiny == ""


def test_with_spinner_stops_the_thread_even_on_error() -> None:
    console = _rec_console()
    try:
        with spinner.with_spinner(console, "Working"):
            raise ValueError("boom")
    except ValueError:
        pass
    # No live spinner thread should survive the context.
    assert not any(t.name == "jack-spinner" and t.is_alive() for t in _threads())


def _rec_console() -> Console:
    return Console(file=StringIO(), force_terminal=False)


def _threads() -> list[threading.Thread]:
    return threading.enumerate()
