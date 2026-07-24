"""Pure HUD segment renderers plus the registry the composer walks.

Each renderer maps ``(state, opts, width)`` to prompt_toolkit ``(style, text)`` fragments,
or ``None`` when it has no data (so a segment is simply omitted -- never a fake ``0%``). The
registry value is ``(renderer, priority)``; the composer drops the lowest-priority segments
first when a row would overflow the terminal width.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from autobot.cli.hud.state import HudState

Fragments = list[tuple[str, str]]
Renderer = Callable[[HudState, dict[str, Any], int], "Fragments | None"]


def human_tokens(n: int) -> str:
    """Compact token count: ``38000`` -> ``38k``; values under 1000 stay as-is."""
    return f"{n // 1000}k" if n >= 1000 else str(n)


def _autonomy(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if not state.autonomy:
        return None
    text = f"{state.autonomy} mode" if opts.get("mode") else state.autonomy
    return [("class:status.key", text)]


def _model(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if not state.model:
        return None
    text = state.model
    if opts.get("provider") and state.provider:
        text = f"{text} ({state.provider})"
    return [("class:status", text)]


def _context(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.window <= 0 or state.used <= 0:
        return None
    frac = min(state.used / state.window, 1.0)  # used can momentarily exceed the window
    pct = round(frac * 100)
    warn = float(opts.get("warn", 0.75))
    crit = float(opts.get("crit", 0.9))

    def depth_color(depth: float) -> str:
        # Color by how deep into the window a point sits: green while there's headroom,
        # amber past the warn line, red past crit -- so the fill ramps in color as it grows.
        return "red" if depth >= crit else "amber" if depth >= warn else "green"

    style = opts.get("style", "bar+pct")
    frags: Fragments = []
    if style in ("pct", "bar+pct"):
        frags.append((f"class:{depth_color(frac)}", f"ctx {pct}%"))
    if style in ("bar", "bar+pct"):
        if frags:
            frags.append(("class:status", " "))
        cells = max(1, int(opts.get("cells", 10)))
        filled = min(cells, max(1, round(frac * cells)))  # any nonzero usage shows >= 1 cell
        # Filled cells, colored by their own depth and grouped into same-color runs; the
        # unfilled remainder is one dim track, so a nearly-empty bar never reads as full.
        i = 0
        while i < filled:
            run_color = depth_color((i + 1) / cells)
            j = i
            while j < filled and depth_color((j + 1) / cells) == run_color:
                j += 1
            frags.append((f"class:{run_color}", "█" * (j - i)))
            i = j
        if filled < cells:
            frags.append(("class:dim", "░" * (cells - filled)))
    return frags


def _tokens(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.window <= 0 or state.used <= 0:
        return None
    return [("class:status", f"{human_tokens(state.used)}/{human_tokens(state.window)}")]


def _git(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if not state.branch:
        return None
    text = state.branch + ("*" if state.dirty else "")
    if opts.get("ahead_behind"):
        if state.ahead:
            text += f" ↑{state.ahead}"
        if state.behind:
            text += f" ↓{state.behind}"
    return [("class:status", text)]


def _cwd(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if not state.cwd:
        return None
    return [("class:status", state.cwd)]


def _cost(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.cost_usd is None:
        return None
    label = opts.get("label")
    value = f"${state.cost_usd:.2f}"
    return [("class:status", f"{label} {value}" if label else value)]


def _mcp(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.mcp_count is None:
        return None
    return [("class:status", f"{state.mcp_count} MCP")]


def _skills(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.skills_count is None:
        return None
    return [("class:status", f"{state.skills_count} skills")]


def _elapsed(state: HudState, opts: dict[str, Any], width: int) -> Fragments | None:
    if state.elapsed_s is None:
        return None
    secs = int(state.elapsed_s)
    text = f"{secs // 60}m{secs % 60}s" if secs >= 60 else f"{secs}s"
    return [("class:status", text)]


# key -> (renderer, priority). Higher priority survives width-gating longer.
SEGMENTS: dict[str, tuple[Renderer, int]] = {
    "context": (_context, 100),
    "model": (_model, 90),
    "autonomy": (_autonomy, 80),
    "git": (_git, 60),
    "cost": (_cost, 55),
    "tokens": (_tokens, 50),
    "mcp": (_mcp, 30),
    "skills": (_skills, 30),
    "elapsed": (_elapsed, 20),
    "cwd": (_cwd, 10),
}
