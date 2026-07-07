"""The inline coding-agent REPL: read a line, drive one turn, print to native scrollback.

A synchronous loop — completed turns are printed with a rich Console (committed to the
terminal's scrollback), and a threaded spinner animates during each blocking daemon call.
All I/O collaborators are injected so the loop is unit-tested without a TTY or a daemon.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from autobot.cli import client, commands, gitdiff, render, spinner
from autobot.cli.classify import classify
from autobot.cli.prompt import Answer, parse_confirm_choice, parse_plan_choice
from autobot.cli.theme import GLYPH_PROMPT
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_Reader = Callable[[str], str | None]
_Spin = Callable[[Any, str], AbstractContextManager[None]]


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
        post: client.Post,
        reader: _Reader,
        console: Any,
        snapshot: Callable[[str], str | None] = gitdiff.snapshot,
        diff_since: Callable[[str, str | None], str | None] = gitdiff.diff_since,
        spin: _Spin = spinner.with_spinner,
    ) -> None:
        """Wire the shell; all collaborators are injectable for tests."""
        self._base_url = base_url
        self._cwd = cwd
        self._post = post
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
        """Run a client-side command; return True to exit the loop."""
        _log.debug("command dispatch name=%s", name)
        res = commands.dispatch(name, args)
        if res.action == "exit":
            return True
        if res.action == "clear":
            self._console.clear()
        else:
            self._console.print(res.text)
        return False

    def _turn(self, text: str) -> None:
        """Echo the user turn, drive plan/pending to done, then print the diff."""
        self._console.print(render.render_user(text))
        snap = self._snapshot(self._cwd)
        verb = spinner.verb_for(self._turn_no)
        _log.info("turn start")
        with self._spin(self._console, verb):
            resp = client.start_turn(self._base_url, text, post=self._post)
        while isinstance(resp, dict) and resp.get("status") in ("plan", "pending"):
            seg = classify(resp)
            self._console.print(render.render_rich(seg))
            ans = self._ask(seg.kind)
            with self._spin(self._console, verb):
                resp = client.answer(self._base_url, ans.value, ans.text, post=self._post)
        if isinstance(resp, str):  # transport/JSON error, already friendly
            _log.error("turn failed: %s", resp)
            self._console.print(resp)
            return
        self._console.print(render.render_rich(classify(resp)))
        if resp.get("status") == "done":
            diff = self._diff_since(self._cwd, snap)
            if diff:
                self._console.print(render.render_diff_rich(diff, width=self._console.width))

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
    Shell(base_url, cwd, post=client._post, reader=reader, console=console).run()
