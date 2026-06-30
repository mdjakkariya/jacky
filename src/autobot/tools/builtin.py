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


def find_tools(intent: str) -> str:
    """Fallback text for the discovery meta-tool.

    The real behavior (search the gated tools, pin the matches, summarize them)
    lives in the LLM turn loop, which intercepts ``find_tools`` calls before they
    reach any registry/gate. This handler exists only so the tool has a valid,
    string-returning handler — it is never dispatched in normal operation.
    """
    return f"Searching for tools matching: {intent}"


FIND_TOOLS = ToolSpec(
    name="find_tools",
    description=(
        "Discover tools that are not currently available to you. Call this the "
        "moment a request needs an action you don't see among your tools (for "
        "example messaging, calendars, code hosting, or any connected app). Pass a "
        "short description of what you want to do as `intent` (e.g. 'send a message "
        "on slack', 'create a github issue'). It returns the matching tools, which "
        "then become available so you can call the right one on your next step. "
        "Prefer this over telling the user you can't do something."
    ),
    parameters={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "A short description of the action you want to perform.",
            }
        },
        "required": ["intent"],
    },
    handler=find_tools,
    risk=Risk.READ_ONLY,
)


def register_builtins(registry: ToolRegistry) -> None:
    """Register all built-in tools into ``registry``."""
    registry.register(GET_TIME)
