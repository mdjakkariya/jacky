"""The mutable HUD snapshot every segment reads.

Deliberately a plain mutable dataclass (not a frozen value object): it is a single
owned scratchpad the app updates in place as feeds arrive (startup context, the live
context event, turn-end usage/git refresh), and segments render from it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class HudState:
    """Everything the HUD segments can render, fed from several sources."""

    autonomy: str = ""
    model: str = ""
    provider: str = ""
    cwd: str = ""
    branch: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    used: int = 0  # current context-window occupancy (tokens); 0 = no data yet
    window: int = 0  # the model's context window (tokens); 0 = unknown
    cost_usd: float | None = None  # session cost estimate (cloud only)
    mcp_count: int | None = None  # configured MCP servers
    skills_count: int | None = None  # discovered skills (not populated in v1 -> omitted)
    elapsed_s: float | None = None  # last turn's wall-clock duration
