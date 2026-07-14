"""The inline coding-agent REPL: read a line, drive one turn, print to native scrollback.

A synchronous loop — completed turns are printed with a rich Console (committed to the
terminal's scrollback), and a threaded spinner animates during each blocking daemon call.
All I/O collaborators are injected so the loop is unit-tested without a TTY or a daemon.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from autobot.cli import client, coder_commands, commands, gitdiff, render, spinner, theme
from autobot.cli.classify import classify
from autobot.cli.prompt import Answer, parse_confirm_choice, parse_plan_choice
from autobot.cli.theme import GLYPH_PROMPT
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_Reader = Callable[[str], str | None]
_Spin = Callable[[Any, str], AbstractContextManager[None]]
_StreamTurn = Callable[[str, str], Iterator[dict[str, Any]]]
_StreamAnswer = Callable[[str, str, str], Iterator[dict[str, Any]]]


def gather_context(cwd: str) -> dict[str, str]:
    """Best-effort status context for the banner/footer (never raises)."""
    branch = _branch(cwd)
    model, autonomy = "?", "?"
    try:
        from autobot.config import Settings

        s = Settings.load()
        model = s.anthropic_model if s.llm_provider == "anthropic" else s.llm_model
        autonomy = s.coding_autonomy
    except Exception:  # config is best-effort for a status line
        _log.debug("could not load settings for context", exc_info=True)
    return {"cwd": _short(cwd), "branch": branch, "model": model, "autonomy": autonomy}


def _branch(cwd: str) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _short(cwd: str) -> str:
    from pathlib import Path

    home = str(Path.home())
    return cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd


class Shell:
    """Drives the inline REPL over the coder daemon's turn protocol."""

    def __init__(
        self,
        base_url: str,
        cwd: str,
        *,
        stream_turn: _StreamTurn = client.stream_turn,
        stream_answer: _StreamAnswer = client.stream_answer,
        reader: _Reader,
        console: Any,
        snapshot: Callable[[str], str | None] = gitdiff.snapshot,
        diff_since: Callable[[str, str | None], str | None] = gitdiff.diff_since,
        spin: _Spin = spinner.with_spinner,
    ) -> None:
        """Wire the shell; all collaborators are injectable for tests."""
        self._base_url = base_url
        self._cwd = cwd
        self._stream_turn = stream_turn
        self._stream_answer = stream_answer
        self._reader = reader
        self._console = console
        self._snapshot = snapshot
        self._diff_since = diff_since
        self._spin = spin
        self._turn_no = 0

    def run(self) -> None:
        """The REPL loop: welcome, then read → dispatch/turn until EOF or /exit."""
        self._console.print(render.render_welcome(gather_context(self._cwd)))
        while True:
            try:
                line = self._reader(f"{GLYPH_PROMPT} ")
            except KeyboardInterrupt:
                continue  # Ctrl-C at idle clears the line
            if line is None:
                break  # EOF / Ctrl-D
            line = line.strip()
            if not line:
                continue
            parsed = commands.parse(line)
            if parsed is not None:
                if self._command(*parsed):
                    break
                continue
            self._turn(line)
            self._turn_no += 1

    def _command(self, name: str, args: str) -> bool:
        """Run a command; return True to exit the loop.

        Daemon-backed / cwd-touching commands go through ``coder_commands.handle``
        first; if it doesn't own the command, fall back to the pure client-side
        ``commands.dispatch`` (``/help /clear /exit``).
        """
        _log.debug("command dispatch name=%s", name)
        rendered = coder_commands.handle(
            name, args, base_url=self._base_url, cwd=self._cwd, width=self._console.width
        )
        if rendered is not None:
            self._console.print(rendered)
            return False
        res = commands.dispatch(name, args)
        if res.action == "exit":
            return True
        if res.action == "clear":
            self._console.clear()
        else:
            self._console.print(res.text)
        return False

    def _turn(self, text: str) -> None:
        """Drive a turn from the event stream: render tool lines live, cards, reply, diff."""
        snap = self._snapshot(self._cwd)
        verb = spinner.verb_for(self._turn_no)
        _log.info("turn start")
        events = self._stream_turn(self._base_url, text)
        while True:
            # Breathing room ABOVE the loading region: print the gap before the spinner
            # starts, so it's there during loading — not popped in after the reply arrives.
            self._console.print()
            phase, printed_activity = self._consume_until_phase(events, verb)
            if phase is None:
                return
            seg = classify(phase)
            if printed_activity:
                # One blank line separates the ⎿ tool/output activity block from the reply
                # or card, so tool-heavy turns read as cleanly as pure-text ones.
                self._console.print()
            if seg.kind in ("plan", "pending"):
                self._console.print(render.render_rich(seg))
                ans = self._ask(seg.kind)
                events = self._stream_answer(self._base_url, ans.value, ans.text)
                continue
            # done / error
            self._console.print(render.render_rich(seg))
            if phase.get("status") == "done":
                diff = self._diff_since(self._cwd, snap)
                if diff:
                    self._console.print()
                    self._console.print(render.render_diff_rich(diff, width=self._console.width))
            self._console.print()
            return

    def _consume_until_phase(self, events: Any, verb: str) -> tuple[dict[str, Any] | None, bool]:
        """Drain streaming (token/tool) events — rendering them live — and return the phase event.

        The spinner runs alone until the *first* streaming event (token or tool) arrives —
        it and the token ``rich.Live`` region must never be active at once, since both drive
        their own ``Live`` on the same console and a nested one simply never paints. On that
        first event the spinner is torn down and the token ``Live`` takes over for the rest
        of the phase: reply tokens accumulate into ``buffer`` and repaint the transient region
        as plain text (``⏺ <buffer>``); tool ``start`` events print as dim ``⎿`` lines above
        it. Because the region is transient, it clears the instant a phase event arrives —
        the caller then prints ``render.render_rich(seg)`` (the finalized markdown reply), so
        the live preview and the finalized text never both linger in the scrollback.

        Returns ``(phase_dict, printed_activity)`` — the phase event (or ``None`` if the
        stream ended without one), and whether any tool/output activity line was committed
        to the scrollback (so the caller can insert one separating blank line before the
        reply).
        """
        from rich.live import Live
        from rich.text import Text

        buffer = ""
        printed_activity = False
        live_region = Live(console=self._console, refresh_per_second=12, transient=True)
        spin_cm = self._spin(self._console, verb)
        spin_cm.__enter__()
        spinning = True
        live: Live | None = None
        try:
            for evt in events:
                if isinstance(evt, dict) and evt.get("status") in (
                    "plan",
                    "pending",
                    "done",
                    "error",
                ):
                    return evt, printed_activity
                seg = classify(evt)
                is_tool_start = seg.kind == "tool" and evt.get("event") == "start"
                if (seg.kind in ("token", "output") or is_tool_start) and spinning:
                    spin_cm.__exit__(None, None, None)
                    spinning = False
                    live = live_region.__enter__()
                if seg.kind == "token":
                    buffer += seg.text
                    assert live is not None
                    live.update(Text(f"{theme.GLYPH_ASSISTANT} {buffer}", style="assistant"))
                elif is_tool_start:
                    self._console.print(render.render_tool(seg))
                    printed_activity = True
                elif seg.kind == "output":
                    # Command output, streamed live under the ⎿ tool line (dim), as it arrives.
                    self._console.print(Text(seg.text, style="tool"))
                    printed_activity = True
        finally:
            exc_info = sys.exc_info()
            if spinning:
                spin_cm.__exit__(*exc_info)
            elif live is not None:
                live_region.__exit__(*exc_info)
        return None, printed_activity

    def _ask(self, kind: str) -> Answer:
        """Read a plan/permission choice, re-asking until it parses; EOF/Ctrl-C → reject."""
        parse = parse_plan_choice if kind == "plan" else parse_confirm_choice
        while True:
            try:
                raw = self._reader("> ")
            except KeyboardInterrupt:
                return Answer("reject") if kind == "plan" else Answer("no")
            if raw is None:
                return Answer("reject") if kind == "plan" else Answer("no")
            ans = parse(raw)
            if ans is None:
                continue
            if ans.value == "refine":  # plan edit: take the follow-up as the refinement
                try:
                    follow = self._reader("what should change? ")
                except KeyboardInterrupt:
                    return Answer("reject")
                if follow is None:
                    return Answer("reject")
                return Answer("refine", follow.strip())
            return ans


def run(base_url: str, cwd: str) -> None:  # pragma: no cover - launches the interactive app
    """Launch the inline REPL against ``base_url`` for the project at ``cwd``."""
    from rich.console import Console

    from autobot.cli.prompt import make_reader, make_session
    from autobot.cli.theme import jack_theme

    console = Console(theme=jack_theme())
    reader = make_reader(make_session(cwd, commands.COMMANDS))
    Shell(
        base_url,
        cwd,
        stream_turn=client.stream_turn,
        stream_answer=client.stream_answer,
        reader=reader,
        console=console,
    ).run()
