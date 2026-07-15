"""The async turn driver, driven by scripted event streams + a FakeSurface (no TTY/daemon)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from autobot.cli import driver as drv
from autobot.cli.prompt import Answer
from tests.unit.support import FakeSurface


async def _agen(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for it in items:
        yield it


def _run(
    events: list[dict[str, Any]],
    answers: dict[tuple[str, str], list[dict[str, Any]]] | None,
    surface: FakeSurface,
    *,
    diff: str | None = None,
) -> None:
    answers = answers or {}

    def answer_stream(value: str, text: str) -> AsyncIterator[dict[str, Any]]:
        return _agen(answers.get((value, text), []))

    d = drv.TurnDriver(
        surface, cwd="/x", snapshot=lambda _c: "SNAP", diff_since=lambda _c, _s: diff
    )
    asyncio.run(d.run_turn(_agen(events), answer_stream, turn_no=0))


def _render_text(renderable: Any) -> str:
    """Render a rich renderable (or str) to plain text for assertions."""
    from rich.console import Console

    if isinstance(renderable, str):
        return renderable
    c = Console(record=True, width=80, force_terminal=True)
    c.print(renderable)
    return c.export_text()


def test_tool_line_and_reply_each_committed_once() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "tool", "event": "start", "name": "read_file", "label": "Read parser.py"},
            {"status": "done", "reply": "Done — 1 file."},
        ],
        None,
        s,
    )
    flat = [_render_text(r) for r in s.commits]
    # The tool line and the reply are each committed EXACTLY once (duplicate-row regression).
    assert sum("Read parser.py" in t for t in flat) == 1
    assert sum("Done — 1 file." in t for t in flat) == 1


def test_done_commits_diff_when_present() -> None:
    s = FakeSurface()
    _run([{"status": "done", "reply": "ok"}], None, s, diff="diff --git a/x b/x\n+new")
    flat = "\n".join(_render_text(r) for r in s.commits)
    assert "new" in flat  # the diff was rendered and committed


def test_activity_is_cleared_at_end_of_turn() -> None:
    s = FakeSurface()
    _run([{"status": "done", "reply": "ok"}], None, s)
    assert s.activity[-1] == ""  # live region cleared when the turn finishes


def test_error_status_event_is_committed() -> None:
    s = FakeSurface()
    _run([{"status": "error", "reply": "couldn't reach the daemon: boom"}], None, s)
    assert any("couldn't reach the daemon" in _render_text(r) for r in s.commits)


def test_plan_approve_then_answer_stream_drives_to_done() -> None:
    s = FakeSurface(answers=[Answer("approve")])
    _run(
        [{"status": "plan", "reply": "1. wrap fetch", "todo": ["wrap fetch"]}],
        {("approve", ""): [{"status": "done", "reply": "Done."}]},
        s,
    )
    flat = "\n".join(_render_text(r) for r in s.commits)
    assert "wrap fetch" in flat and "Done." in flat
    assert s.asked and s.asked[0].kind == "plan"


def test_permission_yes_then_done() -> None:
    s = FakeSurface(answers=[Answer("yes")])
    _run(
        [{"status": "pending", "prompt": "run pytest?"}],
        {("yes", ""): [{"status": "done", "reply": "Ran."}]},
        s,
    )
    assert any("Ran." in _render_text(r) for r in s.commits)
    assert s.asked[0].kind == "pending"


def test_plan_update_deltas_committed_once_per_transition() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "plan_update", "todos": [{"step": "run suite", "status": "in_progress"}]},
            {"type": "plan_update", "todos": [{"step": "run suite", "status": "in_progress"}]},
            {"type": "plan_update", "todos": [{"step": "run suite", "status": "done"}]},
            {"status": "done", "reply": "ok"},
        ],
        None,
        s,
    )
    flat = [_render_text(r) for r in s.commits]
    # dedup: the unchanged in_progress repeat does not re-commit
    assert sum("run suite" in t for t in flat) == 2  # one in_progress, one done


def test_cancelled_mid_turn_commits_interrupted_and_reraises() -> None:
    import pytest

    s = FakeSurface()

    async def _events() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "tool", "event": "start", "name": "run_command", "label": "$ sleep"}
        raise asyncio.CancelledError

    d = drv.TurnDriver(s, cwd="/x", snapshot=lambda _c: None, diff_since=lambda _c, _s: None)

    async def _go() -> None:
        await d.run_turn(_events(), lambda v, t: _agen([]), turn_no=0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_go())
    flat = "\n".join(_render_text(r) for r in s.commits)
    assert "interrupted" in flat.lower()
    assert s.activity[-1] == ""  # live region cleared


def test_aiter_blocking_yields_all_items_in_order() -> None:
    def sync_gen() -> Any:
        yield {"a": 1}
        yield {"a": 2}

    async def _collect() -> list[dict[str, Any]]:
        return [x async for x in drv.aiter_blocking(sync_gen())]

    assert asyncio.run(_collect()) == [{"a": 1}, {"a": 2}]
