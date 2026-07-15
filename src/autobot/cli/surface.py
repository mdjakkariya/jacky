"""The rendering seam the turn driver writes to.

The driver depends only on this Protocol — never on prompt_toolkit or a live terminal — so
the whole turn-drive path is unit-testable with a recording fake. The real implementation
(``cli/app.py::AppSurface``) commits finished lines to native scrollback via
``run_in_terminal`` and paints the live region; tests use ``tests/unit/support.FakeSurface``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from autobot.cli.classify import Segment
    from autobot.cli.prompt import Answer


class Surface(Protocol):
    """Where a turn's output goes: committed scrollback + a transient live region."""

    def commit(self, renderable: Any) -> None:
        """Commit a finished renderable to the terminal's scrollback (permanent)."""
        ...

    def set_activity(self, text: str) -> None:
        """Set the live region's current-activity line (transient; empty clears it)."""
        ...

    def clear_activity(self) -> None:
        """Clear the live region (equivalent to ``set_activity('')``)."""
        ...

    async def ask(self, seg: Segment) -> Answer:
        """Resolve a plan/permission gate and return the user's answer."""
        ...
