"""The prompt_toolkit Application, driven headlessly via a pipe input + DummyOutput."""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from autobot.cli.app import AppSurface, JackApp, parse_gate_answer
from autobot.cli.classify import Segment
from autobot.cli.prompt import Answer


def _run_with_feed(feed_body: Any, *, cwd: str = "/x") -> JackApp:
    """Drive a JackApp with a concurrent feeder coroutine ``feed_body(inp, japp)``; return app."""
    holder: dict[str, JackApp] = {}

    async def noop(_t: str, _n: int) -> None:
        return None

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(cwd=cwd, run_turn=noop, commands={}, input=inp, output=DummyOutput())
            holder["app"] = japp

            async def feed() -> None:
                await feed_body(inp, japp)
                inp.send_text("\x04")  # quit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    return holder["app"]


def _bracketed(text: str) -> str:
    """Wrap ``text`` in the terminal bracketed-paste escape sequence."""
    return f"\x1b[200~{text}\x1b[201~"


def test_bracketed_paste_large_text_collapses_and_expands() -> None:
    big = "\n".join(f"line {i}" for i in range(14))  # > MAX_INPUT_LINES → collapses

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(big))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed)
    assert "[Pasted #1 · 14 lines]" in japp._input.text  # collapsed in the input
    assert japp.expand_pastes(japp._input.text) == big  # expands back to the real content on send


def test_bracketed_paste_small_multiline_shows_inline() -> None:
    small = "line a\nline b\nline c"  # fits in the growing box → not collapsed

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(small))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed)
    assert japp._input.text == small  # shown inline (box grows), no placeholder
    assert "[Pasted" not in japp._input.text


def test_backspace_removes_the_whole_pasted_block() -> None:
    big = "\n".join(f"row {i}" for i in range(14))

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(big))
        await asyncio.sleep(0.1)
        inp.send_text("\x7f")  # Backspace over the placeholder
        await asyncio.sleep(0.1)

    japp = _run_with_feed(feed)
    assert "[Pasted" not in japp._input.text  # the whole placeholder was removed, not one char


def test_up_arrow_recalls_previous_submissions() -> None:
    captured: dict[str, str] = {}

    async def noop(_t: str, _n: int) -> None:
        return None

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=inp, output=DummyOutput())

            async def feed() -> None:
                inp.send_text("first message\r")
                await asyncio.sleep(0.1)
                inp.send_text("second message\r")
                await asyncio.sleep(0.1)
                inp.send_text("\x1b[A")  # ↑ recalls the most recent submission
                await asyncio.sleep(0.1)
                captured["up"] = japp._input.text
                inp.send_text("\x04")

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert captured["up"] == "second message"


def test_bracketed_paste_existing_path_becomes_a_mention(tmp_path: Any) -> None:
    from pathlib import Path

    f = Path(tmp_path) / "notes.md"
    f.write_text("hi", encoding="utf-8")

    async def feed(inp: Any, japp: JackApp) -> None:
        inp.send_text(_bracketed(str(f)))
        await asyncio.sleep(0.15)

    japp = _run_with_feed(feed, cwd=str(tmp_path))
    assert japp._input.text.strip() == f"@{f}"  # a pasted path becomes an @mention


def test_parse_gate_answer_plan_and_permission() -> None:
    plan = Segment("plan", "the plan")
    assert parse_gate_answer(plan, "y") == Answer("approve")
    assert parse_gate_answer(plan, "n") == Answer("reject")
    assert parse_gate_answer(plan, "") == Answer("reject")
    assert parse_gate_answer(plan, "use a retry loop") == Answer("refine", "use a retry loop")
    pending = Segment("pending", "run pytest?")
    assert parse_gate_answer(pending, "yes") == Answer("yes")
    assert parse_gate_answer(pending, "nope") == Answer("no")


def test_submitting_a_line_spawns_a_turn() -> None:
    spawned: list[tuple[str, int]] = []

    async def fake_run_turn(text: str, turn_no: int) -> None:
        spawned.append((text, turn_no))

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=fake_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("hello\r")  # submit one turn
                await asyncio.sleep(0.1)  # let the turn task run before we quit
                inp.send_text("\x04")  # Ctrl-D → EOF → exit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert spawned == [("hello", 0)]


def test_escape_cancels_a_running_turn() -> None:
    cancelled = asyncio.Event()

    async def slow_run_turn(text: str, turn_no: int) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=slow_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("do it\r")  # start a slow turn
                await asyncio.sleep(0.2)  # let the task actually start
                inp.send_text("\x1b")  # ESC → interrupt
                for _ in range(60):  # wait (≤3s) for ESC to actually cancel the turn
                    if cancelled.is_set():
                        break
                    await asyncio.sleep(0.05)
                inp.send_text("\x04")  # Ctrl-D → exit (only after ESC has cancelled)

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert cancelled.is_set()  # ESC — not shutdown teardown — cancelled the turn


def test_appsurface_set_activity_updates_fragments() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    surface = AppSurface(japp, Console(file=StringIO(), force_terminal=False))
    surface.set_activity("Reading parser.py")
    assert japp._activity == "Reading parser.py"
    surface.clear_activity()
    assert japp._activity is None


def test_escape_refills_the_input_with_the_interrupted_text() -> None:
    async def slow_run_turn(text: str, turn_no: int) -> None:
        await asyncio.sleep(10)

    japp: JackApp

    async def _drive() -> str:
        nonlocal japp
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=slow_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("fix the parser bug\r")  # start a turn (input cleared)
                await asyncio.sleep(0.2)
                inp.send_text("\x1b")  # ESC → cancel + refill input
                await asyncio.sleep(0.2)
                inp.send_text("\x04")  # quit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()
        return japp._input.text

    assert asyncio.run(_drive()) == "fix the parser bug"  # refilled for editing/resending


def test_begin_modal_resolves_from_typed_line() -> None:
    got: list[Answer] = []

    async def run_turn(text: str, turn_no: int) -> None:
        got.append(await japp.begin_modal(Segment("plan", "the plan")))

    japp: JackApp

    async def _drive() -> None:
        nonlocal japp
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def feed() -> None:
                inp.send_text("do it\r")  # spawn a turn that opens a modal
                await asyncio.sleep(0.1)
                inp.send_text("y\r")  # answer the gate
                await asyncio.sleep(0.1)
                inp.send_text("\x04")  # exit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert got == [Answer("approve")]


def test_on_task_finished_when_idle_runs_a_continuation_turn() -> None:
    seen: list[str] = []

    async def fake_run_turn(text: str, turn_no: int) -> None:
        seen.append(text)

    async def _drive() -> None:
        with create_pipe_input() as inp:
            japp = JackApp(
                cwd="/x", run_turn=fake_run_turn, commands={}, input=inp, output=DummyOutput()
            )

            async def fire() -> None:
                await asyncio.sleep(0.05)
                japp.on_task_finished([{"id": "task-3", "status": "done"}])
                await asyncio.sleep(0.05)
                inp.send_text("\x04")  # exit

            asyncio.get_running_loop().create_task(fire())
            await japp.run_async()

    asyncio.run(_drive())
    assert seen and "background task" in seen[0].lower()
