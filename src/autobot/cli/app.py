"""The one long-lived prompt_toolkit Application for the coding-agent CLI.

A single render owner: the pinned input + a transient live region are painted by this one
app loop; finished lines are committed to native scrollback via ``run_in_terminal`` (see
``AppSurface``). A submitted line spawns the turn as an asyncio task, so the input stays
pinned mid-turn and ``escape`` **detaches the client** from the turn — it cancels the local
drive coroutine and re-pins the prompt. (It does *not* yet abort the turn server-side; the
daemon has no cancel endpoint, so a tool already running there runs to completion. A
daemon-side abort is a tracked follow-up.)

Plan/permission gates are answered through the *same* pinned input (type ``y``/``n`` or, for
a plan, type the change you want) — a future handshake between the turn task (awaiting the
answer) and the accept handler (resolving it). No nested Application, no dynamic key layers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style

from autobot.cli import live_region, theme
from autobot.cli.prompt import Answer, JackCompleter
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.cli.classify import Segment

_log = get_logger("cli")

_RunTurn = Callable[[str, int], Awaitable[None]]
_TICK_INTERVAL_S = 0.12  # live-region repaint cadence (spinner frame + elapsed seconds)

_STYLE = Style.from_dict(
    {
        "spinner": "#4fd6b8",
        "verb": "bold",
        "dim": "#5b665f",
        "prompt": "#4fd6b8 bold",
        "amber": "#e6b25f",
    }
)

_CONTINUATION_PROMPT = (
    "A background task you started has finished (its result is included above). Continue the "
    "task using that result; if everything is now complete, briefly confirm what happened."
)


def parse_gate_answer(seg: Segment, line: str) -> Answer:
    """Map a typed gate reply to an :class:`Answer` (pure — unit-tested).

    Plan gate: ``y``/``yes`` approves, ``n``/``no``/empty rejects, any other text is taken as
    a refinement. Permission gate: ``y``/``yes`` allows, anything else declines.
    """
    text = line.strip()
    low = text.lower()
    if seg.kind == "plan":
        if low in ("y", "yes"):
            return Answer("approve")
        if low in ("n", "no", ""):
            return Answer("reject")
        return Answer("refine", text)
    return Answer("yes") if low in ("y", "yes") else Answer("no")


class JackApp:
    """Owns the Application: pinned input, live region, gates, and turn-task lifecycle."""

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
        self._exiting = False
        self._task: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        # Live-region state (all painted by _live_content on the app loop — single owner):
        self._verb = live_region.verb_for(0)  # set per turn in _drive
        self._activity: str | None = None  # None = no spinner region; str = sub-activity line
        self._modal_hint: list[tuple[str, str]] | None = None  # gate affordance, when awaiting
        self._turn_started = 0.0
        self._frame_i = 0
        self._modal: asyncio.Future[Answer] | None = None
        self._modal_seg: Segment | None = None
        self._pending_pickups: list[dict[str, Any]] = []
        self._input = Buffer(
            accept_handler=self._on_accept,
            completer=JackCompleter(commands, cwd),
            complete_while_typing=True,
            multiline=False,
        )
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

    @property
    def turn_no(self) -> int:
        """How many turns have been started this session (for the exit footer)."""
        return self._turn_no

    def _live_content(self) -> list[tuple[str, str]]:
        """Compose the live-region fragments from current state (called each paint)."""
        if self._modal_hint is not None:
            return self._modal_hint
        if self._activity is None:
            return []
        elapsed = time.monotonic() - self._turn_started
        frame = theme.SPINNER_FRAMES[self._frame_i % len(theme.SPINNER_FRAMES)]
        width = self.app.output.get_size().columns or 80
        return live_region.live_fragments(self._verb, frame, elapsed, self._activity, width)

    def _build_layout(self) -> Layout:
        live = ConditionalContainer(
            Window(FormattedTextControl(self._live_content)),
            filter=Condition(lambda: self._modal_hint is not None or self._activity is not None),
        )
        glyph = Window(
            FormattedTextControl([("class:prompt", f"{theme.GLYPH_PROMPT} ")]),
            width=2,
            dont_extend_width=True,
            height=1,
        )
        entry = Window(BufferControl(buffer=self._input), height=1)
        row = VSplit([glyph, entry])
        body = FloatContainer(
            HSplit([live, row]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))],
        )
        return Layout(body, focused_element=entry)

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-d")
        def _eof(event: Any) -> None:
            if self._modal is not None and not self._modal.done():
                return  # a gate is pending — a stray Ctrl-D must not quit
            if not self._input.text:
                self._exiting = True  # stop any queued auto-resume from spawning at shutdown
                if self._task is not None and not self._task.done():
                    self._task.cancel()
                event.app.exit()

        @kb.add("escape", eager=True)
        def _interrupt(event: Any) -> None:
            if self._task is not None and not self._task.done():
                self._task.cancel()

        return kb

    def _on_accept(self, buff: Buffer) -> bool:
        # A pending gate consumes the next submitted line as its answer.
        if self._modal is not None and not self._modal.done():
            seg = self._modal_seg
            answer = parse_gate_answer(seg, buff.text) if seg is not None else Answer("no")
            buff.reset()
            self._modal.set_result(answer)
            return False
        text = buff.text.strip()
        if not text or self.busy:
            return False
        buff.reset()
        self._task = asyncio.create_task(self._drive(text, self._turn_no))
        self._turn_no += 1
        return False

    async def _drive(self, text: str, turn_no: int) -> None:
        self._verb = live_region.verb_for(turn_no)
        self._turn_started = time.monotonic()
        self._frame_i = 0
        self._ticker = asyncio.create_task(self._tick())
        try:
            await self._run_turn(text, turn_no)
        except asyncio.CancelledError:
            pass
        except Exception:  # a failed turn must never kill the app loop
            _log.exception("turn crashed turn_no=%d", turn_no)
        finally:
            if self._ticker is not None:
                self._ticker.cancel()
                self._ticker = None
            self._activity = None
            self._modal_hint = None
            self._modal = None
            self._modal_seg = None
            self.app.invalidate()
            if not self._exiting:
                asyncio.get_running_loop().call_soon(self._maybe_pickup)

    async def _tick(self) -> None:
        """Advance the spinner frame and repaint the elapsed timer while a turn runs."""
        try:
            while True:
                self._frame_i += 1
                self.app.invalidate()
                await asyncio.sleep(_TICK_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def begin_modal(self, seg: Segment) -> Answer:
        """Show a gate affordance in the live region and await the typed answer."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Answer] = loop.create_future()
        self._modal = fut
        self._modal_seg = seg
        hint = "[y]es · [n]o · or type a change" if seg.kind == "plan" else "approve? [y]es · [n]o"
        self._modal_hint = [("class:amber", f"  {hint}")]
        self.app.invalidate()
        try:
            return await fut
        finally:
            self._modal_hint = None
            self._modal = None
            self._modal_seg = None
            self.app.invalidate()

    def set_activity(self, text: str) -> None:
        """Set the live region's sub-activity line (the app owns the spinner + verb + timer)."""
        self._activity = text
        self.app.invalidate()

    def clear_activity(self) -> None:
        """Clear the live region."""
        self._activity = None
        self.app.invalidate()

    def on_task_finished(self, events: list[dict[str, Any]]) -> None:
        """Queue finished background tasks and, when idle, run a continuation turn.

        Thread-safe: called from the events-listener thread. Finished-while-busy events are
        buffered and delivered when the current turn ends (see :meth:`_maybe_pickup`).
        """
        if not events:
            return
        loop = self.app.loop
        if loop is None:
            return

        def _enqueue() -> None:
            self._pending_pickups.extend(events)
            self._maybe_pickup()

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:  # loop closing at shutdown — nothing to resume onto
            _log.debug("could not schedule task pickup (loop closing)")

    def _maybe_pickup(self) -> None:
        """If idle and pickups are queued, notice them and run a continuation turn."""
        if self._exiting or self.busy or not self._pending_pickups:
            return
        from autobot.cli import render

        events = self._pending_pickups[:]
        self._pending_pickups.clear()
        self._render_pickup(render.render_task_pickup(events))
        self._task = asyncio.create_task(self._drive(_CONTINUATION_PROMPT, self._turn_no))
        self._turn_no += 1

    def _render_pickup(self, renderable: Any) -> None:
        console = self._pickup_console
        if console is None:  # tests: no console wired
            return
        run_in_terminal(lambda: console.print(renderable))  # pragma: no cover - live terminal

    async def run_async(self) -> None:
        """Run the app to completion (EOF exits)."""
        await self.app.run_async()


class AppSurface:
    """The real ``Surface``: commits to scrollback and paints the app's live region."""

    def __init__(self, japp: JackApp, console: Any) -> None:
        """Bind to a ``JackApp`` and a rich ``Console`` for committed lines."""
        self._japp = japp
        self._console = console

    def commit(self, renderable: Any) -> None:
        """Commit a finished renderable above the app via ``run_in_terminal``.

        The print runs in a scheduled ``run_in_terminal`` task, so it is guarded here (a bad
        renderable must never surface as an unretrieved-task error or crash the turn).
        """

        def _print() -> None:  # pragma: no cover - needs a live terminal
            try:
                self._console.print(renderable)
            except Exception:
                _log.exception("committing a renderable failed; dropping it")

        run_in_terminal(_print)  # pragma: no cover - needs a live terminal

    def set_activity(self, text: str) -> None:
        """Set the live region's current-activity line."""
        self._japp.set_activity(text)

    def clear_activity(self) -> None:
        """Clear the live region."""
        self._japp.clear_activity()

    async def ask(self, seg: Segment) -> Answer:
        """Resolve a plan/permission gate via the pinned-input modal handshake."""
        if seg.kind == "pending":
            from rich.text import Text

            self.commit(Text(seg.text, style="amber"))
        return await self._japp.begin_modal(seg)
