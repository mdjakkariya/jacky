"""Turn flat HUD settings into validated, ordered rows of (segment, opts), and render them.

``resolve_config`` is the single place that knows the presets and normalizes a hand-edited
settings file (unknown keys dropped, unknown preset -> essential, empty override -> preset).
``compose`` renders those rows into width-gated fragment lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.cli.hud.state import HudState
    from autobot.config import Settings

_log = get_logger("hud")

# Preset -> rows; each row is an ordered list of (segment_key, opts). Opts set the per-preset
# richness (e.g. Full shows the provider + ahead/behind; the context bar style per preset).
_PRESETS: dict[str, list[list[tuple[str, dict[str, Any]]]]] = {
    "minimal": [
        [("autonomy", {}), ("model", {}), ("context", {"style": "pct"})],
    ],
    "essential": [
        [
            ("autonomy", {}),
            ("model", {}),
            ("context", {"style": "bar+pct"}),
            ("git", {}),
            ("cwd", {}),
        ],
    ],
    "full": [
        [
            ("autonomy", {"mode": True}),
            ("model", {"provider": True}),
            ("git", {"ahead_behind": True}),
            ("cwd", {}),
        ],
        [
            ("context", {"style": "bar+pct"}),
            ("tokens", {}),
            ("cost", {}),
            ("mcp", {}),
            ("skills", {}),
            ("elapsed", {}),
        ],
    ],
}


def resolve_config(settings: Settings) -> list[list[tuple[str, dict[str, Any]]]]:
    """Normalize flat HUD settings into ordered rows of ``(segment_key, opts)``.

    Returns ``[]`` when the HUD is disabled. Unknown segment keys are dropped (logged);
    an unknown preset falls back to essential; a non-empty ``hud_segments`` overrides the
    preset into a single row. ``hud_options`` is merged over each segment's preset opts, and
    the context bar's color thresholds are injected from settings.
    """
    from autobot.cli.hud.segments import SEGMENTS

    if not settings.hud_enabled:
        return []

    preset = settings.hud_preset if settings.hud_preset in _PRESETS else "essential"
    if settings.hud_preset not in _PRESETS:
        _log.debug("unknown hud_preset=%r -> essential", settings.hud_preset)

    if settings.hud_segments:
        rows: list[list[tuple[str, dict[str, Any]]]] = [[(k, {}) for k in settings.hud_segments]]
    else:
        # Deep-copy the preset rows so per-call opts merging never mutates the module constant.
        rows = [[(k, dict(o)) for k, o in row] for row in _PRESETS[preset]]

    options = settings.hud_options if isinstance(settings.hud_options, dict) else {}
    out: list[list[tuple[str, dict[str, Any]]]] = []
    for row in rows:
        resolved_row: list[tuple[str, dict[str, Any]]] = []
        for key, opts in row:
            if key not in SEGMENTS:
                _log.warning("dropping unknown HUD segment %r", key)
                continue
            merged = dict(opts)
            extra = options.get(key)
            if isinstance(extra, dict):
                merged.update(extra)
            if key == "context":
                merged.setdefault("warn", float(settings.hud_context_warn))
                merged.setdefault("crit", float(settings.hud_context_crit))
            resolved_row.append((key, merged))
        out.append(resolved_row)
    return out


def compose(
    rows: list[list[tuple[str, dict[str, Any]]]],
    state: HudState,
    *,
    width: int,
    separator: str,
) -> list[list[tuple[str, str]]]:
    """Render resolved rows into width-gated fragment lines (one list per visible row).

    Each segment is rendered from ``state``; ``None`` results (no data) are skipped. Surviving
    segments are joined by ``separator``. If a row's plain-text width exceeds ``width``, the
    lowest-priority segments are dropped one at a time until it fits. Rows that render empty
    are omitted entirely.
    """
    from autobot.cli.hud.segments import SEGMENTS

    lines: list[list[tuple[str, str]]] = []
    for row in rows:
        rendered: list[tuple[int, list[tuple[str, str]]]] = []
        for key, opts in row:
            renderer, priority = SEGMENTS[key]
            frags = renderer(state, opts, width)
            if frags:
                rendered.append((priority, frags))
        rendered = _fit(rendered, width, len(separator))
        if not rendered:
            continue
        line: list[tuple[str, str]] = []
        for i, (_prio, frags) in enumerate(rendered):
            if i:
                line.append(("class:status", separator))
            line.extend(frags)
        lines.append(line)
    return lines


def _fit(
    rendered: list[tuple[int, list[tuple[str, str]]]],
    width: int,
    sep_len: int,
) -> list[tuple[int, list[tuple[str, str]]]]:
    """Drop the lowest-priority segments until the joined plain-text width fits ``width``."""

    def plain_width(items: list[tuple[int, list[tuple[str, str]]]]) -> int:
        text = sum(len(t) for _p, frags in items for _s, t in frags)
        seps = sep_len * max(0, len(items) - 1)
        return text + seps

    kept = list(rendered)
    while kept and plain_width(kept) > width:
        # Find the lowest priority (ties: rightmost) and drop it.
        victim = min(range(len(kept)), key=lambda i: (kept[i][0], -i))
        kept.pop(victim)
    return kept
