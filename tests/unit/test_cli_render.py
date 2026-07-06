"""render_plain (stdlib) + render_rich (rich) produce the expected output per Segment."""

from __future__ import annotations

import pytest

from autobot.cli.classify import Segment
from autobot.cli.render import render_diff_plain, render_plain


def test_plain_plan_labels_and_includes_text() -> None:
    out = render_plain(Segment("plan", "1. edit foo", ("edit foo",)))
    assert "PLAN" in out and "1. edit foo" in out


def test_plain_done_is_the_reply() -> None:
    assert render_plain(Segment("done", "All set.")) == "All set."


def test_plain_error_is_flagged() -> None:
    assert "error" in render_plain(Segment("error", "boom")).lower()


def test_diff_plain_passthrough() -> None:
    assert render_diff_plain("+added\n-removed") == "+added\n-removed"


def test_rich_plan_renders_text() -> None:
    pytest.importorskip("rich")
    from rich.console import Console  # type: ignore[import-not-found]

    from autobot.cli.render import render_rich

    console = Console(record=True, width=80)
    console.print(render_rich(Segment("plan", "wrap fetch in retry", ("step",))))
    assert "wrap fetch in retry" in console.export_text()
