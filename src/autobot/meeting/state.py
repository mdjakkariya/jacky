"""Process-wide access to the live meeting-recorder status.

Mirrors :func:`autobot.tools.access.active_policy`: a tiny module-level provider so
context builders (the LLM's per-turn system-context assembler) can read the *current*
meeting state without threading the recorder through every constructor. The recorder
is created in the composition root (:func:`autobot.app.build`), which registers its
``status`` method here; anything that needs the live state reads it via
:func:`meeting_status_snapshot`.

Why this exists: a meeting can be stopped from the drawer's Stop button, which calls
the daemon directly and never passes through the language model. Without a live
readout the model keeps believing "I'm recording" from an earlier turn and refuses to
start a new one. Injecting the real state each turn keeps its view honest.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_provider: Callable[[], dict[str, Any]] | None = None


def set_meeting_status_provider(provider: Callable[[], dict[str, Any]] | None) -> None:
    """Register (or clear with ``None``) the callable returning the recorder's status."""
    global _provider
    _provider = provider


def meeting_status_snapshot() -> dict[str, Any] | None:
    """Return the current meeting status dict, or ``None`` if meetings aren't wired.

    A failing provider is swallowed (returns ``None``) — reading meeting state must
    never break a turn.
    """
    if _provider is None:
        return None
    try:
        return _provider()
    except Exception:
        return None
