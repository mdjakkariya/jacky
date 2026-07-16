"""The async turn driver, driven by scripted event streams + a FakeSurface (no TTY/daemon)."""

from __future__ import annotations

import asyncio
import time
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


def test_blank_line_precedes_reply_after_activity() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "tool", "event": "start", "name": "read_file", "label": "Read a.py"},
            {"status": "done", "reply": "ok"},
        ],
        None,
        s,
    )
    assert len(s.commits) == 3  # ⎿ tool line, a blank spacer, then the ⏺ reply
    assert _render_text(s.commits[1]).strip() == ""  # the spacer is genuinely blank


def test_no_extra_blank_before_reply_when_no_activity() -> None:
    s = FakeSurface()
    _run([{"status": "done", "reply": "ok"}], None, s)
    # No tool activity ran, so the shell's user-message spacer already sits above the reply —
    # the driver must NOT add a second blank (which would double the gap).
    assert len(s.commits) == 1


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


def test_run_command_buffers_output_and_commits_a_card() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "tool", "event": "start", "name": "run_command", "label": "$ npm test"},
            {"type": "output", "text": "PASS a"},
            {"type": "output", "text": "PASS b"},
            {"type": "tool", "event": "end", "name": "run_command", "label": "$ npm test"},
            {"status": "done", "reply": "ok"},
        ],
        None,
        s,
    )
    flat = [_render_text(r) for r in s.commits]
    assert not any("PASS a" in t for t in flat)  # output is NOT dumped into the transcript
    assert s.commands == [("$ npm test", ["PASS a", "PASS b"])]  # buffered, carded on end


def test_gated_command_is_not_echoed_again_before_its_card() -> None:
    # In confirm mode the permission gate already shows the command, so the run_command start
    # must NOT echo it again — only the result card is committed (command shown exactly once).
    s = FakeSurface(answers=[Answer("yes")])
    _run(
        [{"status": "pending", "prompt": "Run this command?\n\n  $ npm test"}],
        {
            ("yes", ""): [
                {"type": "tool", "event": "start", "name": "run_command", "label": "$ npm test"},
                {"type": "output", "text": "PASS"},
                {"type": "tool", "event": "end", "name": "run_command", "label": "$ npm test"},
                {"status": "done", "reply": "ok"},
            ]
        },
        s,
    )
    # The command "$ npm test" is committed by the gate (via ask), not echoed again on start.
    flat = [_render_text(r) for r in s.commits]
    assert sum("$ npm test" in t for t in flat) <= 1  # not duplicated as a ⎿ echo
    assert s.commands == [("$ npm test", ["PASS"])]  # buffered + carded


def test_plan_updates_go_to_live_checklist_not_the_transcript() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "plan_update", "todos": [{"step": "run suite", "status": "in_progress"}]},
            {"type": "plan_update", "todos": [{"step": "run suite", "status": "done"}]},
            {"status": "done", "reply": "ok"},
        ],
        None,
        s,
    )
    flat = [_render_text(r) for r in s.commits]
    assert not any("run suite" in t for t in flat)  # no per-delta ⎿ lines pollute the transcript
    assert s.todos[-1] == [("done", "run suite")]  # the live checklist holds the latest state


def test_update_plan_tool_line_is_suppressed() -> None:
    s = FakeSurface()
    _run(
        [
            {"type": "tool", "event": "start", "name": "update_plan", "label": "Update plan"},
            {"type": "tool", "event": "end", "name": "update_plan", "label": "Update plan"},
            {"status": "done", "reply": "ok"},
        ],
        None,
        s,
    )
    flat = [_render_text(r) for r in s.commits]
    assert not any("Update plan" in t for t in flat)  # the checklist panel is the display


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


def test_aiter_blocking_swallows_close_error_when_cancelled_mid_read() -> None:
    """Cancelling mid-read must not let close()'s 'generator already executing' crash the turn."""
    import threading

    started = threading.Event()

    class _BlockingStream:
        def __iter__(self) -> Any:
            return self

        def __next__(self) -> dict[str, Any]:
            started.set()
            time.sleep(0.3)  # simulate a urllib SSE read blocking in the executor thread
            raise StopIteration

        def close(self) -> None:
            raise ValueError("generator already executing")

    async def _go() -> str:
        async def consume() -> None:
            async for _ in drv.aiter_blocking(_BlockingStream()):
                pass

        task = asyncio.create_task(consume())
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 2.0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled-clean"  # the ValueError from close() was swallowed
        return "unexpected"

    assert asyncio.run(_go()) == "cancelled-clean"
