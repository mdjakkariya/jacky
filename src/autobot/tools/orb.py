"""UI-presence tools: let the assistant tuck the floating orb away on request.

The orb is a thin client of the engine, so these tools don't touch any window
directly — they publish a visibility request through an injected sink (the event
bus), and the orb reacts. Only *hide* is exposed: coming back is automatic, since
saying the wake word re-engages the assistant and the orb reappears.

Registered only when a visibility sink is wired in (i.e. when a UI is present),
so a headless or terminal run never advertises a tool it can't fulfil.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec


def register_orb_tools(registry: ToolRegistry, hide: Callable[[], None]) -> None:
    """Register the ``dismiss`` tool, wired to ``hide`` (publishes a hide request).

    Args:
        registry: The tool registry to register into.
        hide: Called with no arguments to ask the UI to hide itself.
    """

    def dismiss() -> str:
        hide()
        return "hiding now; the wake word brings me back"

    registry.register(
        ToolSpec(
            name="dismiss",
            description=(
                "Hide yourself — tuck the floating orb away — when the user is done "
                "with you for now. Spoken cues: 'go away', 'you're done', 'that's all', "
                "'dismiss', 'hide', 'leave me', 'goodbye', 'bye', 'see you'. After "
                "calling it, say a short, warm goodbye; the orb hides once you finish "
                "speaking, and comes back when they say the wake word. This only hides "
                "the UI — it does not quit, sleep, or shut anything down."
            ),
            parameters={"type": "object", "properties": {}},
            handler=dismiss,
            risk=Risk.WRITE,
            ack="",  # silent — the warm goodbye reply is the response, not a filler
        )
    )
