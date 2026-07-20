"""A tiny thread-safe fan-out so MCP events reach the coder SSE stream.

MCP status/OAuth events are published by the MCP manager's workers (on the MCP
loop thread); the daemon's ``/coder/events`` handler subscribes one callback per
connected CLI. This hub decouples the two: the runner publishes every MCP event
here (as well as to the orb bus), and each SSE subscription taps it alongside the
task-registry listener.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger("mcp")


class CoderEventHub:
    """Fan out event dicts to any number of subscribers (thread-safe, never raises)."""

    def __init__(self) -> None:
        """Start with no subscribers."""
        self._subs: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register ``callback`` for every published event; returns an unsubscribe.

        Args:
            callback: Invoked with each event dict (on the publisher's thread).

        Returns:
            A zero-arg idempotent unsubscribe function.
        """
        with self._lock:
            self._subs.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subs:
                    self._subs.remove(callback)

        return _unsubscribe

    def publish(self, event: dict[str, Any]) -> None:
        """Deliver ``event`` to every subscriber; a bad sink never breaks the rest.

        Args:
            event: The event dict to fan out.
        """
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event)
            except Exception:  # one bad SSE client must not break the others
                _log.debug("coder event subscriber failed", exc_info=True)
