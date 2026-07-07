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
    from rich.console import Console

    from autobot.cli.render import render_rich

    console = Console(record=True, width=80)
    console.print(render_rich(Segment("plan", "wrap fetch in retry", ("step",))))
    assert "wrap fetch in retry" in console.export_text()


def test_rich_dispatch_plan_lists_steps() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.classify import Segment
    from autobot.cli.render import render_rich

    console = Console(record=True, width=80)
    reply = "Here's my plan:\n1. wrap fetch\n2. add test"
    console.print(render_rich(Segment("plan", reply, ("wrap fetch", "add test"))))
    out = console.export_text()
    assert "wrap fetch" in out and "add test" in out and "Proceed" in out


def test_rich_dispatch_pending_is_a_permission_card() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.classify import Segment
    from autobot.cli.render import render_rich

    console = Console(record=True, width=80)
    console.print(render_rich(Segment("pending", "run pytest -q?")))
    assert "pytest -q" in console.export_text()


def test_render_footer_has_context_and_gates_on_width() -> None:
    from autobot.cli.render import render_footer

    ctx = {"model": "qwen3:8b", "autonomy": "plan", "branch": "main", "cwd": "~/proj"}
    wide = render_footer(ctx, width=80)
    assert "qwen3:8b" in wide and "plan" in wide and "main" in wide
    narrow = render_footer(ctx, width=20)
    assert len(narrow) <= 20


def test_render_tool_shows_connector_and_label() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.classify import Segment
    from autobot.cli.render import render_tool

    console = Console(record=True, width=80)
    console.print(render_tool(Segment("tool", "Read a.py")))
    out = console.export_text()
    assert "Read a.py" in out and "⎿" in out
