"""The ``update_plan`` tool: the model publishes its living task checklist.

The coder driver seeds a per-turn ``PlanState`` and installs a sink on the
:data:`~autobot.core.streaming.plan_sink` ContextVar for the act phase. This tool reads
that sink and forwards the model's current checklist to it (the driver updates the state
and emits a ``plan_update`` event for the CLI progress trail). It is **core** (always
advertised during act so the model can update at any point) and **``Risk.READ_ONLY``** (it
reports intent, changing no workspace state), so it never trips the permission gate.
"""

from __future__ import annotations

from autobot.core.streaming import plan_sink
from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry, ToolSpec


def update_plan(todos: list[dict[str, str]] | None) -> str:
    """Publish the task checklist. Never raises; a no-op (with an ack) when no turn listens.

    Args:
        todos: The full checklist, each a ``{"step", "status"}`` dict. ``None`` or empty is
            treated as "no steps given".

    Returns:
        A short ack for the model summarising how many steps are done.
    """
    items = todos or []
    sink = plan_sink.get()
    if sink is not None:
        sink(items)  # driver updates PlanState + emits a plan_update event
    done = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "done")
    return f"Plan updated: {done}/{len(items)} done." if items else "No plan steps given."


def register_plan_tool(registry: ToolRegistry) -> None:
    """Register the ``update_plan`` tool (core, ``Risk.READ_ONLY``) into ``registry``."""
    registry.register(
        ToolSpec(
            name="update_plan",
            description=(
                "Track your task checklist so the user sees progress. Call this right after the "
                "plan is approved to mark the first step 'in_progress', then again each time you "
                "finish a step (mark it 'done' and the next 'in_progress'), and once more when all "
                "steps are 'done'. Pass the FULL list every time. Statuses: pending, in_progress, "
                "done, blocked. This is how the turn knows the task is complete — keep it current."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done", "blocked"],
                                },
                            },
                            "required": ["step", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
            handler=lambda todos=None: update_plan(todos),
            risk=Risk.READ_ONLY,
            core=True,
            ack="Updating the plan.",
        )
    )
