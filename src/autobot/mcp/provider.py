"""Lazy lifecycle holder for the MCP manager, so MCP can be toggled at runtime.

The "Enable MCP connections" setting (``allow_mcp``) is flipped from the Settings
view without restarting the daemon: the daemon resolves the manager through this
provider on every request, and the settings endpoint calls :meth:`set_enabled`
when the flag changes. Enabling builds + starts + wires a fresh
:class:`~autobot.mcp.manager.McpManager`; disabling shuts it down fully (workers
disconnected, loop stopped, tools unregistered). The manager itself (and the heavy
``mcp`` SDK) is built lazily by the injected factory, so this module stays cheap to
import and free of the opt-in extra.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.mcp.manager import McpManager

_log = get_logger("mcp")


class McpProvider:
    """Creates/destroys an :class:`McpManager` on demand (thread-safe).

    Args:
        factory: A zero-arg callable that builds, starts, and fully wires a fresh
            ``McpManager`` (including ``set_confirmer`` then ``connect_enabled``).
            Invoked once each time MCP transitions from disabled to enabled.
    """

    def __init__(self, factory: Callable[[], McpManager]) -> None:
        self._factory = factory
        self._manager: McpManager | None = None
        self._lock = threading.Lock()

    @property
    def manager(self) -> McpManager | None:
        """The live manager, or ``None`` when MCP is disabled."""
        return self._manager

    def set_enabled(self, enabled: bool) -> None:
        """Turn MCP on (create+start) or off (shut down). Idempotent.

        Args:
            enabled: Desired state. Enabling when already on, or disabling when
                already off, is a no-op.
        """
        with self._lock:
            if enabled and self._manager is None:
                self._manager = self._factory()
                _log.info("mcp enabled at runtime")
            elif not enabled and self._manager is not None:
                mgr, self._manager = self._manager, None
                mgr.shutdown()
                _log.info("mcp disabled at runtime")

    def shutdown(self) -> None:
        """Shut down the manager if running (idempotent; for atexit / daemon exit)."""
        self.set_enabled(False)
