"""The prompt_toolkit Application, driven headlessly via a pipe input + DummyOutput."""

from __future__ import annotations

import asyncio
from io import StringIO

from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from autobot.cli.app import AppSurface, JackApp, parse_gate_answer
from autobot.cli.classify import Segment
from autobot.cli.prompt import Answer


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
                await asyncio.sleep(0.1)  # let the task actually start
                inp.send_text("\x1b")  # ESC → cancel
                await asyncio.sleep(0.1)
                inp.send_text("\x04")  # Ctrl-D → exit

            asyncio.get_running_loop().create_task(feed())
            await japp.run_async()

    asyncio.run(_drive())
    assert cancelled.is_set()


def test_appsurface_set_activity_updates_fragments() -> None:
    async def noop(_t: str, _n: int) -> None:
        return None

    japp = JackApp(cwd="/x", run_turn=noop, commands={}, input=DummyInput(), output=DummyOutput())
    surface = AppSurface(japp, Console(file=StringIO(), force_terminal=False))
    surface.set_activity("Reading parser.py")
    assert japp._activity == "Reading parser.py"
    surface.clear_activity()
    assert japp._activity is None


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
