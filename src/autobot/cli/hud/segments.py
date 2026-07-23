"""HUD segments registry (real renderers land in the segments task)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from autobot.cli.hud.state import HudState

Fragments = list[tuple[str, str]]
Renderer = Callable[[HudState, dict[str, Any], int], "Fragments | None"]


def _todo(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    return None


# key -> (renderer, priority). Filled with real renderers in the segments task.
SEGMENTS: dict[str, tuple[Renderer, int]] = dict.fromkeys(
    ("autonomy", "model", "context", "tokens", "git", "cwd", "cost", "mcp", "skills", "elapsed"),
    (_todo, 0),
)
