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
    assert "wrap fetch" in out and "add test" in out
    assert "[y]es" in out and "[e]dit" in out and "[n]o" in out


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


def test_render_sessions_table_has_rows() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.render import render_sessions

    rows = [{"id": "abcdef123456", "model": "qwen3:8b", "cwd": "/x/proj", "mtime": 0.0}]
    console = Console(record=True, width=100)
    console.print(render_sessions(rows))
    out = console.export_text()
    assert "abcdef12" in out and "qwen3:8b" in out and "proj" in out


def test_render_sessions_empty_is_noted() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.render import render_sessions

    console = Console(record=True, width=100)
    console.print(render_sessions([]))
    assert "No sessions" in console.export_text()


def test_render_checkpoints_lists_labels() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.render import render_checkpoints

    rows = [
        {"ref": "refs/jack/checkpoints/1", "sha": "aaa", "label": "before edit"},
        {"ref": "refs/jack/checkpoints/0", "sha": "bbb", "label": "first"},
    ]
    console = Console(record=True, width=100)
    console.print(render_checkpoints(rows))
    out = console.export_text()
    assert "before edit" in out and "first" in out


def test_permission_card_shows_yn_not_numbers() -> None:
    pytest.importorskip("rich")
    from rich.console import Console

    from autobot.cli.render import render_permission_card

    console = Console(record=True, width=80)
    console.print(render_permission_card("Run this command?\n\n  $ npm install"))
    out = console.export_text()
    assert "[y/n]" in out
    assert "[1]" not in out and "Proceed" not in out


def test_render_todo_glyphs_per_status() -> None:
    from rich.console import Console

    from autobot.cli import render

    def _text(status: str, step: str) -> str:
        con = Console(width=80)
        with con.capture() as cap:
            con.print(render.render_todo(status, step))
        return cap.get()

    assert "☑" in _text("done", "run the suite") and "run the suite" in _text(
        "done", "run the suite"
    )
    assert "◐" in _text("in_progress", "y")
    assert "⊘" in _text("blocked", "z")
