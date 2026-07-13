"""Jack Dark: the terminal theme — one source for every color and glyph.

Colors are Jack's own identity (teal accent), not borrowed from any reference tool.
Every renderer takes its styles from :func:`jack_theme`; nothing hard-codes a color.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.theme import Theme

# Gutter grammar + chrome glyphs.
GLYPH_ASSISTANT = "⏺"  # ⏺  assistant turn
GLYPH_USER = "▌"  # ▌  user turn block
GLYPH_TOOL = "⎿"  # ⎿  nested tool result
GLYPH_PROMPT = "❯"  # noqa: RUF001, RUF003  # ❯  prompt / select pointer
RULE_CHAR = "─"  # ─  border-as-rule

# Jack's braille-orbit spinner (a smooth rotation).
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Semantic style tokens (rich style strings). Semantic colors are separate from
# the accent.
STYLES: dict[str, str] = {
    "assistant": "#4fd6b8 bold",
    "user": "#c7d0cb on #1a231f",
    "tool": "#5b665f",
    "prompt": "#4fd6b8 bold",
    "rule": "#5b665f",
    "dim": "#5b665f",
    "teal": "#4fd6b8",
    "green": "#83c878",
    "red": "#e2726f",
    "amber": "#e6b25f",
    "blue": "#74a9e0",
    "violet": "#b892d6",
    "num": "#5b665f",
    "diff_add": "#a7e39a on #14301f",
    "diff_del": "#f0a3a1 on #351a1c",
    "diff_add_word": "#a7e39a on #1f5c34",
    "diff_del_word": "#f0a3a1 on #6b2529",
}


def jack_theme() -> Theme:
    """Build the rich :class:`Theme` from :data:`STYLES`."""
    from rich.theme import Theme

    return Theme(STYLES)
