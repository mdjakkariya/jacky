"""The CLI HUD: a live, segment-based docked status bar."""

from __future__ import annotations

from autobot.cli.hud.compose import compose, resolve_config
from autobot.cli.hud.state import HudState

__all__ = ["HudState", "compose", "resolve_config"]
