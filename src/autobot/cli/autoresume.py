"""Background-task auto-resume: hold the /coder/events stream open, notify the app.

Runs a daemon thread that keeps the daemon's persistent ``/coder/events`` SSE open; when a
backgrounded command finishes it records the event and calls the current *waker* (installed
by ``shell.run`` as ``lambda: japp.on_task_finished(events.poll_completed())``). The app then
hops to its event loop and, when idle, runs a continuation turn on the result. Finished-while-
busy events are queued and delivered when the current turn ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from autobot.cli import client
from autobot.logging_setup import get_logger

_log = get_logger("cli")

_StreamEvents = Callable[[str], Any]  # base_url -> Iterator[event dict]


class BackgroundEvents:
    """Consumes ``/coder/events`` on a thread; surfaces finished tasks + wakes the prompt."""

    def __init__(
        self, base_url: str, *, stream_events: _StreamEvents = client.stream_events
    ) -> None:
        """Wire the listener (``stream_events`` is injectable for tests); call :meth:`start`."""
        self._base_url = base_url
        self._stream_events = stream_events
        self._lock = threading.Lock()
        self._completed: list[dict[str, Any]] = []
        self._waker: Callable[[], None] | None = None
        self._on_mcp: Callable[[dict[str, Any]], None] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the listener thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="jack-events", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Consume the stream, reconnecting if it drops, until :meth:`close`."""
        while not self._stop.is_set():
            try:
                for evt in self._stream_events(self._base_url):
                    if self._stop.is_set():
                        return
                    if evt.get("type") == "task" and evt.get("status") in ("done", "failed"):
                        with self._lock:
                            self._completed.append(evt)
                            waker = self._waker
                        _log.info("background task finished id=%s", evt.get("id"))
                        if waker is not None:
                            waker()
                    elif evt.get("type") in ("mcp_status", "mcp_oauth"):
                        with self._lock:
                            on_mcp = self._on_mcp
                        if on_mcp is not None:
                            on_mcp(evt)
            except Exception:  # a listener error must never crash the CLI — just retry
                _log.debug("events stream errored; will retry", exc_info=True)
            # Stream ended (daemon restart, drop). Pause briefly, then reconnect.
            if self._stop.wait(1.0):
                return

    def set_waker(self, waker: Callable[[], None] | None) -> None:
        """Install (or clear) the callback fired when a task finishes (the prompt-waker)."""
        with self._lock:
            self._waker = waker

    def set_on_mcp(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Install (or clear) the callback fired for live MCP status/OAuth events."""
        with self._lock:
            self._on_mcp = callback

    def pending(self) -> bool:
        """Whether any finished-task events are waiting to be picked up."""
        with self._lock:
            return bool(self._completed)

    def poll_completed(self) -> list[dict[str, Any]]:
        """Drain and return the finished-task events collected so far (``[]`` if none)."""
        with self._lock:
            out = self._completed[:]
            self._completed.clear()
            return out

    def close(self) -> None:
        """Stop the listener thread (best-effort; never blocks long)."""
        self._stop.set()
