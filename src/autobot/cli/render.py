"""Render a :class:`~autobot.cli.classify.Segment` to plain text or a rich renderable.

Plain forms back the one-shot mode and keep rendering testable without a TTY. Rich forms
back the inline shell: the ⏺ assistant gutter, the plan and permission prompts, the welcome
banner, the footer byline, and full-width diffs (via ``diffview``). The user's own turn is
the input-prompt line the shell already leaves in the scrollback, so it isn't re-rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autobot.cli import theme
from autobot.cli.classify import Segment

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text


def render_plain(seg: Segment) -> str:
    """Plain-text rendering of a segment (no ANSI, no external deps)."""
    if seg.kind == "plan":
        return f"PLAN\n{seg.text}"
    if seg.kind == "error":
        return f"Error: {seg.text}"
    if seg.kind == "token":
        return seg.text
    if seg.kind == "tool":
        return f"{theme.GLYPH_TOOL}  {seg.text}"
    return seg.text  # pending / done


def render_rich(seg: Segment) -> RenderableType:
    """Dispatch a segment to its rich renderable by kind."""
    if seg.kind == "plan":
        return render_plan_card(seg.text)
    if seg.kind == "pending":
        return render_permission_card(seg.text)
    if seg.kind == "error":
        from rich.text import Text

        return Text(f"Error: {seg.text}", style="red")
    if seg.kind == "token":
        from rich.text import Text

        return Text(seg.text)
    if seg.kind == "tool":
        return render_tool(seg)
    return render_reply(seg.text)  # done


def render_reply(text: str) -> RenderableType:
    """The assistant turn: a ⏺ gutter dot inline with the reply's markdown.

    A two-column ``Table.grid`` keeps the dot on the same row as the first line of the
    reply (a plain ``Columns`` layout drops it onto its own line).
    """
    from rich.markdown import Markdown
    from rich.table import Table
    from rich.text import Text

    grid = Table.grid(padding=(0, 1))
    grid.add_column()  # gutter
    grid.add_column()  # content
    grid.add_row(Text(theme.GLYPH_ASSISTANT, style="assistant"), Markdown(text))
    return grid


def render_tool(seg: Segment) -> RenderableType:
    """A dim nested tool-activity line: ``⎿ <label>``."""
    from rich.text import Text

    return Text(f"{theme.GLYPH_TOOL}  {seg.text}", style="tool")


def _proceed(*labels: str) -> Text:
    """A ``Proceed?   [1] … [2] …`` choice line with teal-numbered options."""
    from rich.text import Text

    line = Text("Proceed?", style="bold")
    for i, label in enumerate(labels, 1):
        line.append("   ")
        line.append(f"[{i}]", style="teal")
        line.append(f" {label}")
    return line


def render_plan_card(reply: str) -> RenderableType:
    """The plan: the assistant's numbered plan, then the approve / edit / cancel choices.

    The reply already contains the numbered steps, so they are shown once (via the reply)
    and never re-listed.
    """
    from rich.console import Group
    from rich.text import Text

    return Group(render_reply(reply), Text(""), _proceed("Yes", "Edit", "No"))


def render_permission_card(prompt_text: str) -> RenderableType:
    """The permission prompt: the (amber) request, then the run / decline choices."""
    from rich.console import Group
    from rich.text import Text

    return Group(Text(prompt_text, style="amber"), Text(""), _proceed("Yes, run it", "No"))


def render_welcome(ctx: dict[str, str]) -> RenderableType:
    """The startup banner: a rule-framed title + the live context line + a tip."""
    from rich.console import Group
    from rich.text import Text

    title = Text(f"{theme.RULE_CHAR * 3} Jack ", style="teal")
    title.append(theme.RULE_CHAR * 40, style="dim")
    ctx_line = Text("  ")
    ctx_line.append(ctx.get("model", "?"), style="teal")
    ctx_line.append(f"  ·  {ctx.get('autonomy', '?')}", style="amber")
    ctx_line.append(f"  ·  {ctx.get('branch', '?')}", style="blue")
    ctx_line.append(f"  ·  {ctx.get('cwd', '?')}", style="dim")
    tip = Text("  tip  type / for commands, @ to add a file", style="dim")
    return Group(title, ctx_line, tip)


def render_footer(ctx: dict[str, str], width: int) -> str:
    """A width-gated ``model · autonomy · branch · cwd`` status byline (plain str)."""
    parts = [
        ctx.get("model", ""),
        ctx.get("autonomy", ""),
        ctx.get("branch", ""),
        ctx.get("cwd", ""),
    ]
    full = "  ·  ".join(p for p in parts if p)
    if len(full) <= width:
        return full
    short = "  ·  ".join(p for p in (ctx.get("autonomy", ""), ctx.get("branch", "")) if p)
    return short[:width]


def render_diff_plain(diff: str) -> str:
    """Plain diff: return as-is (already unified-diff text)."""
    return diff


def render_diff_rich(diff: str, *, width: int = 80) -> RenderableType:
    """Rich diff: full-width colored bars with word-level highlight (via diffview)."""
    from autobot.cli import diffview

    return diffview.render_diff(diff, width=width)
