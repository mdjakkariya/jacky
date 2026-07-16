"""The one full-screen prompt_toolkit Application for the coding-agent CLI.

Full-screen so the input + status bar are **docked at the bottom** by construction, with a
scrollable transcript above and a transient live region (spinner / current tool / gate
affordance) between them. Finished lines are rendered (rich → ANSI) into the transcript
buffer — no ``run_in_terminal`` (which is CPR-dependent and renders the input mid-screen in
inline mode). The turn runs as a cancellable asyncio task, so the input stays pinned mid-turn
and ``escape`` detaches the client (cancels the local drive; the daemon has no abort endpoint
yet — a tracked follow-up).

Plan/permission gates are answered through the *same* pinned input (type ``y``/``n`` or, for a
plan, type the change you want) — a future handshake between the turn task (awaiting the
answer) and the accept handler (resolving it). No nested Application, no dynamic key layers.

Tradeoff vs. the old inline shell: the transcript scrolls *within* the app (PgUp/PgDn/mouse),
not the terminal's native scrollback — the cost of a truly pinned bottom.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from io import StringIO
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
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
from prompt_toolkit.widgets import Frame

from autobot.cli import live_region, theme
from autobot.cli.prompt import Answer, JackCompleter
from autobot.cli.theme import jack_theme
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
        "status": "#c7d0cb bg:#1a231f",
        "status.key": "#4fd6b8 bg:#1a231f",
        "inputframe": "#4fd6b8",  # teal border around the input box
    }
)

_CONTINUATION_PROMPT = (
    "A background task you started has finished (its result is included above). Continue the "
    "task using that result; if everything is now complete, briefly confirm what happened."
)


def render_ansi(renderable: Any, width: int) -> str:
    """Render a rich renderable (or str) to an ANSI string for the transcript.

    A left/right margin is applied to every committed block so transcript content lines up
    with the input box's prompt glyph (col 2) and never sits flush against the terminal edge.
    """
    buf = StringIO()
    from rich.console import Console
    from rich.padding import Padding

    Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        theme=jack_theme(),
        width=max(20, width),
        soft_wrap=False,
    ).print(Padding(renderable, (0, 1, 0, 2)))  # (top, right, bottom, left)
    return buf.getvalue()


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
    """A full-screen app: scrollable transcript, live region, docked input + status bar."""

    def __init__(
        self,
        *,
        cwd: str,
        run_turn: _RunTurn,
        commands: dict[str, str],
        context: dict[str, str] | None = None,
        intro: Any | None = None,
        input: Any | None = None,
        output: Any | None = None,
    ) -> None:
        """Wire the app; ``run_turn(text, turn_no)`` is the injected turn coroutine.

        ``context`` populates the status bar (model/autonomy/branch); ``intro`` seeds the
        transcript (the welcome banner). ``input``/``output`` are forwarded to the
        ``Application`` (tests inject a pipe input + ``DummyOutput``).
        """
        self._cwd = cwd
        self._run_turn = run_turn
        self._commands = commands
        self._context = context or {}
        self._intro = intro
        self._seeded = False
        self._turn_no = 0
        self._exiting = False
        self._task: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._transcript = ""  # accumulated ANSI text shown in the scrollable region
        # Live-region state (painted by _live_content on the app loop — single owner):
        self._verb = live_region.verb_for(0)
        self._activity: str | None = None
        self._modal_hint: list[tuple[str, str]] | None = None
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
        self._transcript_window = Window(
            FormattedTextControl(self._transcript_text, focusable=False),
            wrap_lines=True,
        )
        self.app: Application[None] = Application(
            layout=self._build_layout(),
            key_bindings=merge_key_bindings([load_key_bindings(), self._bindings()]),
            style=_STYLE,
            full_screen=True,  # docks input + status bar at the bottom, transcript above
            mouse_support=True,
            input=input,
            output=output,
        )

    @property
    def busy(self) -> bool:
        """True while a turn task is running."""
        return self._task is not None and not self._task.done()

    @property
    def turn_no(self) -> int:
        """How many turns have been started this session."""
        return self._turn_no

    def _cols(self) -> int:
        try:
            return self.app.output.get_size().columns or 100
        except Exception:
            return 100

    def _transcript_text(self) -> ANSI:
        """The transcript region's content (seed the intro lazily once width is known)."""
        if not self._seeded and self._intro is not None:
            self._transcript = render_ansi(self._intro, self._cols())
            self._seeded = True
        return ANSI(self._transcript)

    def _status_text(self) -> list[tuple[str, str]]:
        """The docked status bar: just the session context (autonomy · model · branch).

        Deliberately hint-free — no esc/​/help/​^C clutter. The 'esc to interrupt' hint lives
        in the loading line, shown only while a turn runs (where it's actionable).
        """
        autonomy = self._context.get("autonomy", "auto")
        model = self._context.get("model", "")
        branch = self._context.get("branch", "")
        rest = "  ·  ".join(p for p in (model, branch) if p)
        frags: list[tuple[str, str]] = [("class:status.key", f"  {autonomy} mode")]
        if rest:
            frags.append(("class:status", f"  ·  {rest}"))
        frags.append(("class:status", "  "))
        return frags

    def _live_content(self) -> list[tuple[str, str]]:
        """Compose the live-region fragments from current state (called each paint)."""
        if self._modal_hint is not None:
            return self._modal_hint
        if self._activity is None:
            return []
        elapsed = time.monotonic() - self._turn_started
        frame = theme.SPINNER_FRAMES[self._frame_i % len(theme.SPINNER_FRAMES)]
        return live_region.live_fragments(self._verb, frame, elapsed, self._activity, self._cols())

    def _build_layout(self) -> Layout:
        live = ConditionalContainer(
            Window(FormattedTextControl(self._live_content), dont_extend_height=True),
            filter=Condition(lambda: self._modal_hint is not None or self._activity is not None),
        )
        glyph = Window(
            FormattedTextControl([("class:prompt", f"{theme.GLYPH_PROMPT} ")]),
            width=2,
            dont_extend_width=True,
            height=1,
        )
        entry = Window(BufferControl(buffer=self._input), height=1)
        # A bordered input box (a distinct widget) so the input reads as separate from the
        # transcript above and the status bar below — not one clumped block.
        input_box = Frame(VSplit([Window(width=1), glyph, entry]), style="class:inputframe")
        status = Window(FormattedTextControl(self._status_text), height=1, style="class:status")
        body = FloatContainer(
            HSplit([self._transcript_window, live, input_box, status]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))],
        )
        return Layout(body, focused_element=entry)

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-d")
        def _quit(event: Any) -> None:
            if self._modal is not None and not self._modal.done():
                return  # a gate is pending — a stray quit key must not exit
            self._exiting = True  # blocks any queued auto-resume from spawning at shutdown
            if self._task is not None and not self._task.done():
                self._task.cancel()  # cancel the in-flight turn, then quit
            event.app.exit()

        @kb.add("escape", eager=True)
        def _interrupt(event: Any) -> None:
            if self._task is not None and not self._task.done():
                self._task.cancel()

        @kb.add("pageup")
        def _pgup(event: Any) -> None:
            self._transcript_window.vertical_scroll = max(
                0, self._transcript_window.vertical_scroll - 10
            )

        @kb.add("pagedown")
        def _pgdn(event: Any) -> None:
            self._transcript_window.vertical_scroll += 10

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

    def append_transcript(self, renderable: Any) -> None:
        """Render ``renderable`` (rich → ANSI) into the transcript and tail to the bottom."""
        try:
            self._transcript += render_ansi(renderable, self._cols())
        except Exception:  # a bad renderable must never crash the turn
            _log.exception("rendering a transcript line failed; dropping it")
            return
        self._transcript_window.vertical_scroll = 10**9  # clamped to bottom at render (tail)
        self.app.invalidate()

    def clear_transcript(self) -> None:
        """Empty the transcript region (the ``/clear`` command)."""
        self._transcript = ""
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
        self.append_transcript(render.render_task_pickup(events))
        self._task = asyncio.create_task(self._drive(_CONTINUATION_PROMPT, self._turn_no))
        self._turn_no += 1

    async def run_async(self) -> None:
        """Run the app to completion (Ctrl-C / Ctrl-D at idle exits)."""
        await self.app.run_async()


class AppSurface:
    """The real ``Surface``: appends to the transcript and paints the app's live region."""

    def __init__(self, japp: JackApp, console: Any = None) -> None:
        """Bind to a ``JackApp``. ``console`` is unused (kept for call-site compatibility)."""
        self._japp = japp

    def commit(self, renderable: Any) -> None:
        """Commit a finished renderable into the scrollable transcript."""
        self._japp.append_transcript(renderable)

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
