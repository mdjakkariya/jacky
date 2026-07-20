"""The model-facing ``undo`` tool: revert the workspace to the most recent checkpoint.

Thin wrapper over the same restore closure the CLI ``/undo`` uses (built in ``app.py`` over
:mod:`autobot.orchestrator.checkpoint`), so the model can roll a bad change back to the
snapshot taken before the current task's first edit instead of trying to patch forward.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger("coder")


def register_undo_tool(registry: ToolRegistry, undo: Callable[[], tuple[bool, str]]) -> None:
    """Register the ``undo`` tool, delegating to ``undo`` (``() -> (ok, message)``)."""

    def _handler() -> str:
        ok, message = undo()
        _log.info("undo ok=%s", ok)
        return message

    registry.register(
        ToolSpec(
            name="undo",
            description=(
                "Revert the workspace to the most recent checkpoint — undoing the file changes "
                "made during the current task (a checkpoint is taken before the first edit of "
                "each task). Use this to roll back when your edits went wrong, instead of trying "
                "to patch forward. Affects files only; does nothing if there's no checkpoint yet."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_handler,
            risk=Risk.DESTRUCTIVE,
            ack="Undoing the last change.",
        )
    )
