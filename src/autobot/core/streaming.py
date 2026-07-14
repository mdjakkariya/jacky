"""A neutral seam for streaming a running tool's output to the human, live.

The permission gate and tool registry hand a tool its arguments, not the turn's event
channel — but a long command should show its output as it runs. ``output_sink`` is a
:class:`~contextvars.ContextVar` the agent harness sets around each tool execution: a tool
(today only ``run_command``) reads it and writes each output line to it. It lives in
``core`` so both the agent layer (which sets it) and the tools layer (which reads it) can
import it without a layering cycle. ``None`` when no turn is executing a tool.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

#: Set by the harness around each tool call to a ``line -> None`` sink; ``None`` otherwise.
output_sink: ContextVar[Callable[[str], None] | None] = ContextVar("output_sink", default=None)

#: Set by the coder driver around the act phase to a ``todos -> None`` sink; ``None`` otherwise.
plan_sink: ContextVar[Callable[[list[dict[str, str]]], None] | None] = ContextVar(
    "plan_sink", default=None
)
