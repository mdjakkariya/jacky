"""The one long-lived prompt_toolkit Application for the coding-agent CLI.

A single render owner: the pinned input + a transient live region are painted by this one
app loop; finished lines are committed to native scrollback via ``run_in_terminal`` (see
``AppSurface``). A submitted line spawns the turn as an asyncio task, so the input stays
pinned mid-turn and ``escape`` can cancel the task (real interrupt).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

from autobot.cli import live_region, theme
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_RunTurn = Callable[[str, int], Awaitable[None]]

_STYLE = Style.from_dict(
    {"spinner": "#4fd6b8", "verb": "bold", "dim": "#5b665f", "prompt": "#4fd6b8 bold"}
)

_CONTINUATION_PROMPT = (
    "A background task you started has finished (its result is included above). Continue the "
    "task using that result; if everything is now complete, briefly confirm what happened."
)


class JackApp:
    """Owns the Application: pinned input, live region, and turn-task lifecycle."""

    def __init__(
        self,
        *,
        cwd: str,
        run_turn: _RunTurn,
        commands: dict[str, str],
        input: Any | None = None,
        output: Any | None = None,
        pickup_console: Any | None = None,
    ) -> None:
        """Wire the app; ``run_turn(text, turn_no)`` is the injected turn coroutine.

        ``input``/``output`` are forwarded to the ``Application`` (tests inject a pipe input
        + ``DummyOutput``); ``pickup_console`` renders background-task pickup notices.
        """
        self._cwd = cwd
        self._run_turn = run_turn
        self._commands = commands
        self._pickup_console = pickup_console
        self._turn_no = 0
        self._task: asyncio.Task[None] | None = None
        self._activity_frags: list[tuple[str, str]] = []
        self._input = Buffer(accept_handler=self._on_accept, multiline=False)
        self.app: Application[None] = Application(
            layout=self._build_layout(),
            key_bindings=merge_key_bindings([load_key_bindings(), self._bindings()]),
            style=_STYLE,
            full_screen=False,  # inline: output flows into native scrollback
            erase_when_done=False,
            input=input,
            output=output,
        )

    @property
    def busy(self) -> bool:
        """True while a turn task is running."""
        return self._task is not None and not self._task.done()

    def _build_layout(self) -> Layout:
        live = ConditionalContainer(
            Window(FormattedTextControl(lambda: self._activity_frags)),
            filter=Condition(lambda: bool(self._activity_frags)),
        )
        glyph = Window(
            FormattedTextControl([("class:prompt", f"{theme.GLYPH_PROMPT} ")]),
            width=2,
            dont_extend_width=True,
            height=1,
        )
        entry = Window(BufferControl(buffer=self._input), height=1)
        row = VSplit([glyph, entry])
        return Layout(HSplit([live, row]), focused_element=entry)

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-d")
        def _eof(event: Any) -> None:
            if not self._input.text:
                event.app.exit()

        @kb.add("escape", eager=True)
        def _interrupt(event: Any) -> None:
            if self._task is not None and not self._task.done():
                self._task.cancel()

        return kb

    def _on_accept(self, buff: Buffer) -> bool:
        text = buff.text.strip()
        if not text or self.busy:
            return False
        buff.reset()
        self._task = asyncio.create_task(self._drive(text, self._turn_no))
        self._turn_no += 1
        return False

    async def _drive(self, text: str, turn_no: int) -> None:
        try:
            await self._run_turn(text, turn_no)
        except asyncio.CancelledError:
            pass
        except Exception:  # a failed turn must never kill the app loop
            _log.exception("turn crashed turn_no=%d", turn_no)
        finally:
            self._activity_frags = []
            self.app.invalidate()

    def set_activity_fragments(self, frags: list[tuple[str, str]]) -> None:
        """Replace the live region's fragments and repaint."""
        self._activity_frags = frags
        self.app.invalidate()

    def on_task_finished(self, events: list[dict[str, Any]]) -> None:
        """Idle-only auto-resume: schedule a continuation turn for finished background tasks.

        Thread-safe: called from the events-listener thread. Ignored while a turn is running
        (delivered on the next idle instead).
        """
        if self.busy or not events:
            return
        loop = self.app.loop
        if loop is None:
            return
        from autobot.cli import render

        def _spawn() -> None:
            if self.busy:
                return
            self._render_pickup(render.render_task_pickup(events))
            self._task = asyncio.create_task(self._drive(_CONTINUATION_PROMPT, self._turn_no))
            self._turn_no += 1

        loop.call_soon_threadsafe(_spawn)

    def _render_pickup(self, renderable: Any) -> None:
        console = self._pickup_console
        if console is None:  # tests: no console wired
            return
        run_in_terminal(lambda: console.print(renderable))  # pragma: no cover - live terminal

    async def begin_modal(self, seg: Any) -> Any:  # pragma: no cover - interactive modal
        """Show a y/e/n (plan) or y/n (permission) affordance; await the resolved Answer.

        The temporary key layer + affordance rendering are terminal-interactive; the
        plan/permission *behavior* is covered at the driver level (see test_cli_driver).
        """
        from autobot.cli.prompt import Answer

        fut: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._modal_future = fut
        return await fut

    async def run_async(self) -> None:
        """Run the app to completion (EOF exits)."""
        await self.app.run_async()


class AppSurface:
    """The real ``Surface``: commits to scrollback and paints the app's live region."""

    def __init__(self, japp: JackApp, console: Any) -> None:
        """Bind to a ``JackApp`` and a rich ``Console`` for committed lines."""
        self._japp = japp
        self._console = console
        self._verb = live_region.verb_for(0)

    def commit(self, renderable: Any) -> None:
        """Commit a finished renderable above the app via ``run_in_terminal``."""
        run_in_terminal(  # pragma: no cover - needs a live terminal
            lambda: self._console.print(renderable)
        )

    def set_activity(self, text: str) -> None:
        """Paint the live region's spinner + current-activity line."""
        width = self._japp.app.output.get_size().columns or 80
        frame = theme.SPINNER_FRAMES[0]
        self._japp.set_activity_fragments(
            live_region.live_fragments(self._verb, frame, 0.0, text, width)
        )

    def clear_activity(self) -> None:
        """Clear the live region."""
        self._japp.set_activity_fragments([])

    async def ask(self, seg: Any) -> Any:  # pragma: no cover - interactive modal
        """Resolve a plan/permission gate via the app's in-app modal."""
        return await self._japp.begin_modal(seg)
