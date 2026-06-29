"""Built-in tools shipped with the assistant.

Phase 0 has exactly one trivial, read-only tool (``get_time``) — enough to prove
the model emits clean tool-call JSON. Add new tools here (or in their own
modules) and register them in :func:`register_builtins`.
"""

from __future__ import annotations

from datetime import datetime

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec


def get_time() -> str:
    """Return the current local date and time as a human-readable string."""
    return datetime.now().strftime("%A, %d %B %Y, %I:%M:%S %p")


GET_TIME = ToolSpec(
    name="get_time",
    description="Get the current local date and time.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=get_time,
    risk=Risk.READ_ONLY,
    core=True,
)


def register_builtins(registry: ToolRegistry) -> None:
    """Register all built-in tools into ``registry``."""
    registry.register(GET_TIME)
