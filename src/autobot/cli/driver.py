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

from autobot.cli import render, theme
from autobot.cli.classify import classify
from autobot.cli.surface import Surface
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_AnswerStream = Callable[[str, str], "AsyncIterator[dict[str, Any]]"]


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
        _log.info("turn start turn_no=%d", turn_no)
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
                    ans = await self._surface.ask(seg)
                    events = answer_stream(ans.value, ans.text)
                    continue
                self._surface.commit(render.render_rich(seg))  # done / error
                if phase.get("status") == "done":
                    diff = self._diff_since(self._cwd, snap)
                    if diff:
                        self._surface.commit(render.render_diff_rich(diff, width=100))
                return
        except asyncio.CancelledError:
            from rich.text import Text

            self._surface.commit(Text(f"{theme.GLYPH_TOOL} interrupted", style="tool"))
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
        prev: dict[str, str] = {}
        self._surface.set_activity("")  # spinner only until a tool starts
        async for evt in events:
            status = evt.get("status")
            if status in ("plan", "pending", "done", "error"):
                return evt
            if evt.get("type") == "plan_update":
                self._commit_plan_updates(evt, prev)
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
        transcript: while it runs, output is buffered and shown as a rolling live preview (last
        few lines); when it ends, one compact card is committed and the full output is stashed
        for on-demand expand. Other tools just commit their ⎿ label on start.
        """
        seg = classify(evt)
        name = evt.get("name")
        event = evt.get("event")
        if seg.kind == "tool" and event == "start":
            if name == "run_command":
                self._cmd_label = seg.text  # "$ <command>"
                self._cmd_lines = []
                self._surface.set_activity(self._cmd_label)
            else:
                self._surface.commit(render.render_tool(seg))
                self._surface.set_activity(seg.text[:60])  # the tool now running
        elif seg.kind == "tool" and event == "end":
            if name == "run_command" and self._cmd_label is not None:
                self._surface.commit_command(self._cmd_label, self._cmd_lines)
                self._cmd_label = None
                self._cmd_lines = []
        elif seg.kind == "output":
            self._cmd_lines.append(seg.text)
            preview = "\n".join(self._cmd_lines[-4:])  # last 4 lines, shown live
            label = self._cmd_label or ""
            self._surface.set_activity(f"{label}\n{preview}" if label else preview)

    def _commit_plan_updates(self, evt: dict[str, Any], prev: dict[str, str]) -> None:
        """Commit ◐/☑/⊘ delta lines for changed todo steps (dedup via ``prev``)."""
        for todo in evt.get("todos") or []:
            step = str(todo.get("step", ""))
            status = str(todo.get("status", ""))
            if step and prev.get(step) != status and status in ("in_progress", "done", "blocked"):
                self._surface.commit(render.render_todo(status, step))
            if step:
                prev[step] = status


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
