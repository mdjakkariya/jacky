"""The inline shell drive loop, driven by a scripted reader + fake event streams (no TTY/daemon)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from autobot.cli import shell, spinner
from autobot.cli.prompt import Answer
from autobot.cli.theme import GLYPH_ASSISTANT, jack_theme


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


def test_command_routes_daemon_backed_to_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autobot.cli import coder_commands

    calls: list[str] = []

    def fake_handle(name: str, args: str, **kw: Any) -> str | None:
        calls.append(name)
        return "HANDLED" if name == "/diff" else None

    monkeypatch.setattr(coder_commands, "handle", fake_handle)
    sh, console = _make(["/diff", None], {"start": [], "answer": []}, tmp_path)
    sh.run()
    assert calls[0] == "/diff"
    assert "HANDLED" in console.export_text()


def test_command_falls_back_to_pure_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autobot.cli import coder_commands

    monkeypatch.setattr(coder_commands, "handle", lambda *a, **k: None)
    sh, console = _make(["/help", None], {"start": [], "answer": []}, tmp_path)
    sh.run()
    assert "/exit" in console.export_text()  # help text still renders


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


def test_loading_gap_is_printed_before_output_not_after_reply(tmp_path: Path) -> None:
    """The blank gap sits above the loading region, not squeezed in before the reply."""

    class _RecCon(Console):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self.prints: list[bool] = []  # True = printed content, False = printed a blank

        def print(self, *args: Any, **kwargs: Any) -> None:
            self.prints.append(bool(args))
            super().print(*args, **kwargs)

    console = _RecCon(record=True, width=80, theme=jack_theme(), force_terminal=True)
    stream_turn, stream_answer = _streams(
        {
            "start": [
                {"type": "tool", "event": "start", "name": "write_file", "label": "Edited foo"},
                {"status": "done", "reply": "Done."},
            ]
        }
    )
    shell.Shell(
        "http://x",
        str(tmp_path),
        stream_turn=stream_turn,
        stream_answer=stream_answer,
        reader=_scripted_reader(["build foo", None]),
        console=console,
        snapshot=lambda _c: None,
        diff_since=lambda _c, _b: None,
        spin=_noop_spin,
    ).run()
    # log[0] is the welcome banner; the turn's prints follow. The first must be the blank
    # gap, then the tool line, then the reply — with no blank wedged between tool and reply.
    turn = console.prints[1:]
    assert turn[:3] == [False, True, True]


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


class _FakeLive:
    """Stand-in for ``rich.live.Live`` that records each ``update`` instead of drawing.

    Patched in for :func:`rich.live.Live` via monkeypatch, since ``_consume_until_phase``
    imports ``Live`` locally on each call — the patched module attribute is what it binds.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.updates: list[str] = []

    def __enter__(self) -> _FakeLive:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def update(self, renderable: object) -> None:
        self.updates.append(str(renderable))


def test_stream_tokens_render_live_then_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token events repaint a live preview buffer; the ``done`` phase finalizes as markdown."""
    fakes: list[_FakeLive] = []

    def make_fake(*args: object, **kwargs: object) -> _FakeLive:
        live = _FakeLive(*args, **kwargs)
        fakes.append(live)
        return live

    monkeypatch.setattr("rich.live.Live", make_fake)

    turns = {
        "start": [
            {"type": "token", "text": "Hel"},
            {"type": "token", "text": "lo!"},
            {"status": "done", "reply": "Hello!"},
        ],
    }

    def stream_turn(_b: str, _t: str) -> Iterator[dict[str, Any]]:
        return iter(turns["start"])

    def stream_answer(_b: str, _v: str, _t: str = "") -> Iterator[dict[str, Any]]:
        return iter([])

    console = _console()
    sh = shell.Shell(
        "http://x",
        str(tmp_path),
        stream_turn=stream_turn,
        stream_answer=stream_answer,
        reader=_scripted_reader(["hi", None]),
        console=console,
        snapshot=lambda _c: None,
        diff_since=lambda _c, _b: None,
        spin=_noop_spin,
    )
    sh.run()

    assert len(fakes) == 1  # one Live region for the one turn
    assert fakes[0].updates == [f"{GLYPH_ASSISTANT} Hel", f"{GLYPH_ASSISTANT} Hello!"]
    out = console.export_text()
    assert "Hello!" in out  # the done phase's markdown reply is what persists


def test_stream_tokens_paint_with_the_real_spinner(tmp_path: Path) -> None:
    """Regression test: the spinner and the token Live must never both be active.

    Uses the REAL ``spinner.with_spinner`` (not the no-op fake the other tests inject)
    so this exercises actual nested-``Live`` semantics. If the token ``Live`` is opened
    while the spinner's ``Live`` is still active, ``rich`` marks it "nested"; a nested,
    transient ``Live`` never paints its content on ``stop()`` — so the streamed buffer
    text would never reach the console at all, only the finalized phase reply would.
    The phase's reply is deliberately distinct from the buffered tokens so the two
    sources can't be confused: this test fails against a nested structure and passes
    once the spinner is torn down before the token Live starts.
    """
    turns = {
        "start": [
            {"type": "token", "text": "Hel"},
            {"type": "token", "text": "lo!"},
            {"status": "done", "reply": "Done."},
        ],
    }

    def stream_turn(_b: str, _t: str) -> Iterator[dict[str, Any]]:
        return iter(turns["start"])

    def stream_answer(_b: str, _v: str, _t: str = "") -> Iterator[dict[str, Any]]:
        return iter([])

    console = _console()
    sh = shell.Shell(
        "http://x",
        str(tmp_path),
        stream_turn=stream_turn,
        stream_answer=stream_answer,
        reader=_scripted_reader(["hi", None]),
        console=console,
        snapshot=lambda _c: None,
        diff_since=lambda _c, _b: None,
        spin=spinner.with_spinner,  # the real, threaded spinner — not the no-op fake
    )
    sh.run()

    out = console.export_text()
    assert "Done." in out  # the finalized phase reply always prints
    assert "Hello!" in out  # the live-streamed buffer must have actually painted too


def test_turn_renders_streamed_output_lines(tmp_path: Path) -> None:
    # A run_command turn: tool start, two live output lines, then done. All three must
    # reach the scrollback, and the streamed output shows before the reply.
    turns = {
        "start": [
            {"type": "tool", "event": "start", "name": "run_command", "label": "$ npm test"},
            {"type": "output", "text": "PASS a.spec.ts"},
            {"type": "output", "text": "PASS b.spec.ts"},
            {"status": "done", "reply": "All tests passed."},
        ],
        "answer": [],
    }
    sh, console = _make([None], turns, tmp_path)
    sh._turn("run the tests")
    text = console.export_text()
    assert "PASS a.spec.ts" in text
    assert "PASS b.spec.ts" in text
    assert "All tests passed." in text
