"""The inline shell drive loop, driven by a scripted reader + fake post (no TTY/daemon)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
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


def _make(
    lines: list[str | None], responses: list[dict[str, Any]], tmp_path: Path
) -> tuple[shell.Shell, Console]:
    resp_it = iter(responses)

    def post(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        return next(resp_it)

    console = _console()
    sh = shell.Shell(
        "http://x",
        str(tmp_path),
        post=post,
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
        [
            {"status": "plan", "reply": "Here's my plan:\n1. wrap fetch", "todo": ["wrap fetch"]},
            {"status": "done", "reply": "Done."},
        ],
        tmp_path,
    )
    sh.run()
    out = console.export_text()
    assert "wrap fetch" in out and "Done." in out


def test_pending_yes_done(tmp_path: Path) -> None:
    sh, console = _make(
        ["run tests", "1", None],
        [
            {"status": "pending", "prompt": "run pytest?"},
            {"status": "done", "reply": "Ran."},
        ],
        tmp_path,
    )
    sh.run()
    out = console.export_text()
    assert "pytest" in out and "Ran." in out


def test_help_command_renders_without_a_turn(tmp_path: Path) -> None:
    sh, console = _make(["/help", None], [], tmp_path)
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
        post=lambda _url, _payload, _timeout: {},
        reader=reader,
        console=console,
        snapshot=lambda _cwd: None,
        diff_since=lambda _cwd, _b: None,
        spin=_noop_spin,
    )
    assert sh._ask("plan") == Answer("reject")
