"""Render a :class:`~autobot.cli.classify.Segment` to plain text or a rich renderable.

Plain forms back the one-shot mode and keep rendering testable without a TTY. Rich forms
back the inline shell: the gutter grammar (⏺ / ▌ / ⎿), border-as-rule plan and permission
cards, the welcome banner, the footer byline, and full-width diffs (via ``diffview``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autobot.cli import theme
from autobot.cli.classify import Segment

if TYPE_CHECKING:
    from rich.console import RenderableType


def render_plain(seg: Segment) -> str:
    """Plain-text rendering of a segment (no ANSI, no external deps)."""
    if seg.kind == "plan":
        return f"PLAN\n{seg.text}"
    if seg.kind == "error":
        return f"Error: {seg.text}"
    return seg.text  # pending / done


def render_rich(seg: Segment) -> RenderableType:
    """Dispatch a segment to its rich renderable by kind."""
    if seg.kind == "plan":
        return render_plan_card(seg.text, seg.todo)
    if seg.kind == "pending":
        return render_permission_card(seg.text)
    if seg.kind == "error":
        from rich.text import Text

        return Text(f"Error: {seg.text}", style="red")
    return render_reply(seg.text)  # done


def render_user(text: str) -> RenderableType:
    """The user turn block: a ▌-edged line in the user style."""
    from rich.text import Text

    return Text(f"{theme.GLYPH_USER} {text}", style="user")


def render_reply(text: str) -> RenderableType:
    """The assistant turn: a ⏺ gutter dot followed by markdown."""
    from rich.columns import Columns
    from rich.markdown import Markdown
    from rich.text import Text

    return Columns(
        [Text(theme.GLYPH_ASSISTANT, style="assistant"), Markdown(text)],
        padding=(0, 1),
        expand=False,
    )


def render_plan_card(reply: str, todo: tuple[str, ...]) -> RenderableType:
    """A border-as-rule plan card: the reply, the numbered steps, then the choices."""
    from rich.console import Group
    from rich.text import Text

    parts: list[RenderableType] = [render_reply(reply)]
    parts.append(Text(f"{theme.RULE_CHAR} Plan " + theme.RULE_CHAR * 48, style="teal"))
    for i, step in enumerate(todo, 1):
        parts.append(Text(f" {i}. {step}", style="none"))
    parts.append(Text(theme.RULE_CHAR * 54, style="dim"))
    parts.append(Text("Proceed?  [1] Yes  [2] Edit  [3] No", style="teal"))
    return Group(*parts)


def render_permission_card(prompt_text: str) -> RenderableType:
    """A border-as-rule, amber permission card with the yes/no choices."""
    from rich.console import Group
    from rich.text import Text

    return Group(
        Text(f"{theme.RULE_CHAR} Permission " + theme.RULE_CHAR * 42, style="amber"),
        Text(f" {prompt_text}", style="amber"),
        Text(theme.RULE_CHAR * 54, style="dim"),
        Text("Proceed?  [1] Yes, run it  [2] No", style="teal"),
    )


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
