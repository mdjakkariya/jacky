"""The async turn driver: consume the daemon SSE event stream and drive one turn.

Depends only on the ``Surface`` seam and injected event streams, so it is fully unit-tested
with no TTY and no daemon. It commits finished lines to scrollback (``surface.commit``),
keeps the live region's activity current (``surface.set_activity``), resolves plan/permission
gates (``await surface.ask``), and — on cancellation — commits an ``interrupted`` line and
leaves the transcript intact.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any

from autobot.cli import live_region, render, theme
from autobot.cli.classify import classify
from autobot.cli.surface import Surface
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_AnswerStream = Callable[[str, str], "AsyncIterator[dict[str, Any]]"]
_APPROVE = frozenset({"yes", "y", "approve", "once", "session"})  # gate answers that proceed
_EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit"})  # stream an inline diff


class TurnDriver:
    """Drives one user turn from an SSE event stream against a ``Surface``."""

    def __init__(
        self,
        surface: Surface,
        *,
        cwd: str,
        snapshot: Callable[[str], str | None],
        diff_since: Callable[[str, str | None], str | None],
    ) -> None:
        """Wire the driver; ``snapshot``/``diff_since`` mirror ``cli/gitdiff``."""
        self._surface = surface
        self._cwd = cwd
        self._snapshot = snapshot
        self._diff_since = diff_since
        self._cmd_label: str | None = None  # the running command's label ($ …) while buffering
        self._cmd_lines: list[str] = []  # its captured output (shown as a live preview)
        self._gated = False  # a permission gate just showed the command → don't echo it again
        self._diff_lines: list[str] = []  # an edit tool's streamed unified-diff lines
        self._showed_diff = False  # an inline per-edit diff was rendered this turn

    async def run_turn(
        self,
        events: AsyncIterator[dict[str, Any]],
        answer_stream: _AnswerStream,
        *,
        turn_no: int,
    ) -> None:
        """Drive a turn: commit tool/output lines, resolve gates, commit reply + diff."""
        snap = self._snapshot(self._cwd)
        self._cmd_label, self._cmd_lines = None, []  # no command buffering carried across turns
        self._gated = False
        self._diff_lines, self._showed_diff = [], False
        _log.info("turn start turn_no=%d", turn_no)
        # Spacing is owned by the transcript composer: every committed block is separated from
        # its neighbours by a blank line, so the driver just commits blocks (no manual blanks).
        try:
            while True:
                phase = await self._consume_until_phase(events)
                if phase is None:
                    return
                seg = classify(phase)
                if seg.kind == "plan":
                    self._surface.commit(render.render_reply(seg.text))  # the plan is content
                    ans = await self._surface.ask(seg)
                    events = answer_stream(ans.value, ans.text)
                    continue
                if seg.kind == "pending":
                    self._gated = True  # the gate shows the command; don't echo it again on start
                    ans = await self._surface.ask(seg)
                    if ans.value not in _APPROVE and self._cmd_label is not None:
                        # A rejected command never runs → show it in red so the denial is
                        # unmissable, and drop its (empty) result card by forgetting the label.
                        from rich.text import Text

                        self._surface.commit(Text(f"{theme.NEST_INDENT}✗ {self._cmd_label}", "red"))
                        self._cmd_label, self._cmd_lines, self._gated = None, [], False
                    events = answer_stream(ans.value, ans.text)
                    continue
                self._surface.commit(render.render_rich(seg))  # done / error
                if phase.get("status") == "done" and not self._showed_diff:
                    # Fallback whole-turn diff only when no per-edit diff was shown inline (e.g.
                    # a run_command changed files); edit-tool changes already showed their diffs.
                    diff = self._diff_since(self._cwd, snap)
                    if diff:
                        self._surface.commit(render.render_diff_rich(diff, width=100))
                return
        except asyncio.CancelledError:
            from rich.text import Text

            self._surface.commit(Text(f"{theme.NEST_INDENT}interrupted", style="tool"))
            _log.info("turn interrupted turn_no=%d", turn_no)
            raise
        finally:
            self._surface.clear_activity()

    async def _consume_until_phase(
        self, events: AsyncIterator[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Drain events, committing activity lines and keeping the live region current.

        The app owns the spinner + verb + timer; the driver only supplies the sub-activity
        (the current tool label). Returns the first phase event
        (``plan``/``pending``/``done``/``error``), or ``None`` if the stream ended without
        one (e.g. a dropped connection).
        """
        self._surface.set_activity(live_region.DEFAULT_ACTION)  # "Working" until a tool starts
        async for evt in events:
            status = evt.get("status")
            if status in ("plan", "pending", "done", "error"):
                return evt
            if evt.get("type") == "plan_update":
                self._surface.set_activity("Planning")  # the model is managing its plan
                self._push_todos(evt)  # live checklist under the spinner (not committed lines)
                continue
            try:
                self._commit_activity(evt)
            except Exception:  # a bad renderable must never crash the turn
                _log.exception("render failed for event; continuing turn")
            # token events: consumed, not painted (the live region already signals progress).
        return None

    def _commit_activity(self, evt: dict[str, Any]) -> None:
        """Handle a tool start/end or command-output event.

        A ``run_command`` is special-cased so its (possibly huge) output never bloats the
        transcript: while it runs, output is buffered silently; when it ends, one compact card
        is committed and the full output is stashed for on-demand expand. The live region shows
        only the current action (e.g. ``Running command…``) — never a rolling output preview.
        Other tools just commit their ⎿ label on start.
        """
        seg = classify(evt)
        name = str(evt.get("name") or "")
        event = evt.get("event")
        if seg.kind == "tool" and event == "start":
            if name == "run_command":
                # Never echo a ⎿ line for a command on start — it's shown once by the gate
                # (confirm) or by the result card (auto). Just start buffering its output.
                self._cmd_label = seg.text  # "$ <command>"
                self._cmd_lines = []
            elif name == "update_plan":
                pass  # the checklist itself is the display (live panel); no "Update plan" line
            else:
                self._surface.commit(render.render_tool(seg))  # a dim, indented verb line
                if name in _EDIT_TOOLS:
                    self._diff_lines = []  # collect this edit's streamed diff, shown on end
            self._surface.set_activity(live_region.action_label(name))  # "Reading file", etc.
        elif seg.kind == "tool" and event == "end":
            if name == "run_command" and self._cmd_label is not None:
                # Show the command in the card only when NO gate already showed it (auto mode).
                self._surface.commit_command(self._cmd_label, self._cmd_lines, gated=self._gated)
                self._cmd_label = None
                self._cmd_lines = []
                self._gated = False  # consumed by this command
            elif name in _EDIT_TOOLS and self._diff_lines:
                diff = "\n".join(self._diff_lines)
                self._surface.commit(render.render_diff_rich(diff, width=100))
                self._diff_lines = []
                self._showed_diff = True  # skip the fallback whole-turn diff (no duplication)
            self._surface.set_activity(live_region.DEFAULT_ACTION)  # back to "Working"
        elif seg.kind == "output":
            # An edit tool streams its diff; a command streams its output. Route by tool name.
            if name in _EDIT_TOOLS:
                self._diff_lines.append(seg.text)
            else:
                self._cmd_lines.append(seg.text)  # buffered for the card; not previewed live

    def _push_todos(self, evt: dict[str, Any]) -> None:
        """Send the model's full checklist to the live region (shown under the spinner).

        Each ``plan_update`` event carries the whole list, so we replace it wholesale rather
        than committing a line per delta — that keeps the transcript free of ``Update plan``
        churn while the checklist updates in place.
        """
        todos = [
            (str(t.get("status", "")), str(t.get("step", "")))
            for t in evt.get("todos") or []
            if t.get("step")
        ]
        self._surface.set_todos(todos)


async def aiter_blocking(
    sync_iter: Iterable[dict[str, Any]],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Wrap a blocking sync iterator (the daemon SSE generator) as an async iterator.

    Each ``next()`` runs in the loop's default executor so the daemon's blocking ``urllib``
    read never stalls the event loop that is also painting the pinned input + live region.
    On cancellation (esc-to-interrupt) the underlying stream is closed so the ``urllib``
    response socket — and any executor thread parked in ``next()`` — is not leaked.
    """
    loop = loop or asyncio.get_running_loop()
    it: Iterator[dict[str, Any]] = iter(sync_iter)
    sentinel: Any = object()
    try:
        while True:
            item = await loop.run_in_executor(None, lambda: next(it, sentinel))
            if item is sentinel:
                return
            yield item
    finally:
        close = getattr(sync_iter, "close", None)
        if callable(close):
            try:
                close()  # release the underlying urllib SSE response
            except (ValueError, RuntimeError):
                # Cancelled mid-read: the generator is still executing in the executor thread,
                # so it can't be closed now ("generator already executing"). It's released when
                # that blocked next() finally returns. Swallow so the CancelledError survives.
                _log.debug("stream close deferred (generator busy in executor)")
