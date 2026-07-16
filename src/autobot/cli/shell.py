"""The inline coding-agent CLI: one pinned-input prompt_toolkit app over the daemon turns.

Completed turns commit to native scrollback; a single live region shows in-flight activity;
esc interrupts. The heavy lifting lives in ``app`` (the Application + ``AppSurface``),
``driver`` (the async turn drive loop over the ``Surface`` seam), and ``live_region`` (the
frame composer). This module is the composition root: build the console + app, wire the turn
closure and background auto-resume, and run. The pure helpers (context banner, ``@``-mention
expansion, slash-command routing, session footer) are unit-tested without a TTY or a daemon.
"""

from __future__ import annotations

import subprocess
from typing import Any

from autobot.cli import coder_commands, commands, debug_report, mentions
from autobot.logging_setup import get_logger

_log = get_logger("cli")


def gather_context(cwd: str) -> dict[str, str]:
    """Best-effort status context for the banner (never raises)."""
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


def expand_mentions(text: str, cwd: str) -> tuple[str, list[str]]:
    """Resolve ``@path`` mentions into bounded file context.

    Returns ``(resolved_text, attached_names)``; the text is unchanged when nothing matched.
    """
    attached = mentions.find_mentions(text)
    if not attached:
        return text, []
    return mentions.resolve_mentions(text, cwd), attached


def route_command(
    name: str, args: str, *, base_url: str, cwd: str, width: int
) -> tuple[Any | None, str]:
    """Route a slash command; return ``(renderable_or_text | None, action)``.

    ``action`` is ``""`` (just render the first element), ``"clear"``, or ``"exit"``.
    Daemon-backed commands (``/diff`` …) go through ``coder_commands.handle`` first; the
    client-side trio (``/help /clear /exit``) falls back to ``commands.dispatch``.
    """
    rendered = coder_commands.handle(name, args, base_url=base_url, cwd=cwd, width=width)
    if rendered is not None:
        return rendered, ""
    res = commands.dispatch(name, args)
    if res.action == "exit":
        return None, "exit"
    if res.action == "clear":
        return None, "clear"
    return res.text, ""


def print_session_footer(console: Any, cwd: str, turns: int) -> None:
    """On exit, point at the transcript + how to get a shareable debug report.

    Only after real work (≥1 turn) — so just opening and quitting stays quiet.
    """
    if turns == 0:
        return
    from rich.text import Text

    transcript = debug_report.newest_transcript(cwd)
    console.print()
    if transcript is not None:
        console.print(Text(f"session transcript: {transcript}", style="dim"))
    console.print(
        Text(
            "stuck or something off? run  jack debug  here for a shareable report to paste.",
            style="dim",
        )
    )


def run(base_url: str, cwd: str) -> None:  # pragma: no cover - launches the interactive app
    """Launch the pinned-input coding-agent app against ``base_url`` for ``cwd``."""
    import asyncio

    from rich.console import Console
    from rich.text import Text

    from autobot.cli import client, gitdiff, render, theme
    from autobot.cli.app import AppSurface, JackApp
    from autobot.cli.autoresume import BackgroundEvents
    from autobot.cli.driver import TurnDriver, aiter_blocking
    from autobot.cli.theme import jack_theme

    console = Console(theme=jack_theme())  # only for the post-exit session footer
    context = gather_context(cwd)
    events = BackgroundEvents(base_url)
    holder: dict[str, JackApp] = {}

    async def run_turn(text: str, turn_no: int) -> None:
        japp = holder["app"]
        surface = AppSurface(japp)
        surface.commit(Text(""))  # a blank line separates this turn from the previous one
        surface.commit(Text(f"{theme.GLYPH_PROMPT} {text}", style="prompt"))  # echo the ask
        parsed = commands.parse(text)
        if parsed is not None:
            out, action = route_command(*parsed, base_url=base_url, cwd=cwd, width=japp._cols())
            if action == "exit":
                japp._exiting = True
                japp.app.exit()
                return
            if action == "clear":
                japp.clear_transcript()
                return
            if out is not None:
                surface.commit(out)
            return
        resolved, attached = expand_mentions(text, cwd)
        if attached:
            surface.commit(
                Text(f"{theme.GLYPH_TOOL}  attached: {', '.join(attached)}", style="tool")
            )
        driver = TurnDriver(
            surface, cwd=cwd, snapshot=gitdiff.snapshot, diff_since=gitdiff.diff_since
        )

        def answer_stream(value: str, atext: str) -> Any:
            return aiter_blocking(client.stream_answer(base_url, value, atext))

        await driver.run_turn(
            aiter_blocking(client.stream_turn(base_url, resolved)),
            answer_stream,
            turn_no=turn_no,
        )

    japp = JackApp(
        cwd=cwd,
        run_turn=run_turn,
        commands=commands.COMMANDS,
        context=context,
        intro=render.render_welcome(context),
    )
    holder["app"] = japp

    def _waker() -> None:
        # Fired on the events-listener thread. Only drain once the app loop exists, so a task
        # finishing during startup isn't drained-then-dropped; on_task_finished hops to the loop.
        if japp.app.loop is not None:
            japp.on_task_finished(events.poll_completed())

    events.set_waker(_waker)
    events.start()
    try:
        asyncio.run(japp.run_async())
    finally:
        events.close()
        print_session_footer(console, cwd, japp.turn_no)
