"""The inline shell drive loop, driven by a scripted reader + fake event streams (no TTY/daemon)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from rich.console import Console

from autobot.cli import shell
from autobot.cli.prompt import Answer
from autobot.cli.theme import jack_theme


def _console() -> Console:
    return Console(record=True, width=80, theme=jack_theme(), force_terminal=True)


def _scripted_reader(lines: list[str | None]) -> Callable[[str], str | None]:
    it = iter(lines)

    def reader(_prompt: str) -> str | None:
        try:
            return next(it)
        except StopIteration:
            return None

    return reader


def _noop_spin(_console: Console, _verb: str) -> AbstractContextManager[None]:
    return contextlib.nullcontext()


def _streams(
    turns: dict[str, list[dict[str, Any]]],
) -> tuple[
    Callable[[str, str], Iterator[dict[str, Any]]],
    Callable[[str, str, str], Iterator[dict[str, Any]]],
]:
    """Build ``stream_turn``/``stream_answer`` fakes from a scripted ``{"start", "answer"}`` map.

    Mirrors the daemon's SSE event stream without real I/O: ``stream_turn`` replays
    ``turns["start"]`` and each ``stream_answer`` call replays ``turns["answer"]`` — enough
    for the single plan/pending -> done cycles these tests script.
    """

    def stream_turn(_base: str, _text: str) -> Iterator[dict[str, Any]]:
        return iter(turns["start"])

    def stream_answer(_base: str, _value: str, _text: str = "") -> Iterator[dict[str, Any]]:
        return iter(turns["answer"])

    return stream_turn, stream_answer


def _make(
    lines: list[str | None], turns: dict[str, list[dict[str, Any]]], tmp_path: Path
) -> tuple[shell.Shell, Console]:
    stream_turn, stream_answer = _streams(turns)
    console = _console()
    sh = shell.Shell(
        "http://x",
        str(tmp_path),
        stream_turn=stream_turn,
        stream_answer=stream_answer,
        reader=_scripted_reader(lines),
        console=console,
        snapshot=lambda _cwd: None,
        diff_since=lambda _cwd, _b: None,
        spin=_noop_spin,
    )
    return sh, console


def test_plan_approve_done(tmp_path: Path) -> None:
    sh, console = _make(
        ["add retry", "1", None],
        {
            "start": [
                {
                    "status": "plan",
                    "reply": "Here's my plan:\n1. wrap fetch",
                    "todo": ["wrap fetch"],
                }
            ],
            "answer": [{"status": "done", "reply": "Done."}],
        },
        tmp_path,
    )
    sh.run()
    out = console.export_text()
    assert "wrap fetch" in out and "Done." in out


def test_pending_yes_done(tmp_path: Path) -> None:
    sh, console = _make(
        ["run tests", "1", None],
        {
            "start": [{"status": "pending", "prompt": "run pytest?"}],
            "answer": [{"status": "done", "reply": "Ran."}],
        },
        tmp_path,
    )
    sh.run()
    out = console.export_text()
    assert "pytest" in out and "Ran." in out


def test_help_command_renders_without_a_turn(tmp_path: Path) -> None:
    sh, console = _make(["/help", None], {"start": [], "answer": []}, tmp_path)
    sh.run()
    assert "/help" in console.export_text()


def test_ask_refine_followup_ctrl_c_cancels(tmp_path: Path) -> None:
    """Ctrl-C at refine follow-up cancels the turn instead of crashing."""
    calls = iter(["2"])  # "2" = edit/refine; next read raises

    def reader(_prompt: str) -> str | None:
        try:
            return next(calls)
        except StopIteration:
            raise KeyboardInterrupt from None

    console = _console()
    sh = shell.Shell(
        "http://x",
        str(tmp_path),
        reader=reader,
        console=console,
        snapshot=lambda _cwd: None,
        diff_since=lambda _cwd, _b: None,
        spin=_noop_spin,
    )
    assert sh._ask("plan") == Answer("reject")


def test_stream_plan_approve_done_with_tool_line(tmp_path: Path) -> None:
    """Tool-activity events render live (before the final reply) alongside the phase cards."""
    sh, console = _make(
        ["edit foo", "1", None],
        {
            "start": [
                {"status": "plan", "reply": "Here's my plan:\n1. edit foo", "todo": ["edit foo"]}
            ],
            "answer": [
                {"type": "tool", "event": "start", "name": "edit_file", "label": "Edited foo"},
                {"status": "done", "reply": "Edited foo."},
            ],
        },
        tmp_path,
    )
    sh.run()
    out = console.export_text()
    assert "edit foo" in out and "Edited foo" in out and "Edited foo." in out


def test_stream_error_event_renders_in_red(tmp_path: Path) -> None:
    """An ``error``-status event (e.g. transport failure) renders without crashing the shell."""
    sh, console = _make(
        ["do it", None],
        {"start": [{"status": "error", "reply": "I couldn't reach the coder daemon: boom"}]},
        tmp_path,
    )
    sh.run()
    assert "couldn't reach the coder daemon" in console.export_text()


def test_stream_ends_without_phase_prints_nothing_and_does_not_crash(tmp_path: Path) -> None:
    """An event stream that ends without a phase event (e.g. a dropped connection) is safe."""
    sh, console = _make(["do it", None], {"start": []}, tmp_path)
    sh.run()
    assert console.export_text() is not None  # ran to completion without raising
