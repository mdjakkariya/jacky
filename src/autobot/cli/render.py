"""Render a :class:`~autobot.cli.classify.Segment` to plain text or a rich renderable.

Plain forms back the one-shot mode and keep rendering testable without a TTY. Rich forms
back the TUI; they import ``rich`` lazily so the package imports without the ``tui`` extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autobot.cli.classify import Segment

if TYPE_CHECKING:
    from rich.console import RenderableType  # type: ignore[import-not-found]


def render_plain(seg: Segment) -> str:
    """Plain-text rendering of a segment (no ANSI, no external deps)."""
    if seg.kind == "plan":
        return f"PLAN\n{seg.text}"
    if seg.kind == "error":
        return f"Error: {seg.text}"
    return seg.text  # pending / done


def render_rich(seg: Segment) -> RenderableType:
    """Rich rendering of a segment: plan/error/pending as panels, done as markdown."""
    from rich.markdown import Markdown  # type: ignore[import-not-found]
    from rich.panel import Panel  # type: ignore[import-not-found]
    from rich.text import Text  # type: ignore[import-not-found]

    if seg.kind == "plan":
        return Panel(Markdown(seg.text), title="Plan", border_style="cyan")
    if seg.kind == "pending":
        return Panel(Text(seg.text), title="Confirm", border_style="yellow")
    if seg.kind == "error":
        return Panel(Text(seg.text), title="Error", border_style="red")
    return Markdown(seg.text)  # done


def render_diff_plain(diff: str) -> str:
    """Plain diff: return as-is (already unified-diff text)."""
    return diff


def render_diff_rich(diff: str) -> RenderableType:
    """Rich diff: syntax-highlighted unified diff."""
    from rich.syntax import Syntax  # type: ignore[import-not-found]

    return Syntax(diff, "diff", theme="ansi_dark", word_wrap=True)
