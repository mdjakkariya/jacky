"""Jack Dark theme: glyph constants and a rich Theme built from STYLES."""

from __future__ import annotations

from autobot.cli import theme


def test_glyph_constants_are_expected() -> None:
    assert theme.GLYPH_ASSISTANT == "⏺"  # ⏺
    assert theme.GLYPH_USER == "▌"  # ▌
    assert theme.GLYPH_TOOL == "⎿"  # ⎿
    assert theme.GLYPH_PROMPT == "❯"  # noqa: RUF001, RUF003  # ❯
    assert len(theme.SPINNER_FRAMES) == 10  # braille orbit


def test_styles_cover_the_semantic_names() -> None:
    for name in (
        "assistant",
        "user",
        "prompt",
        "rule",
        "dim",
        "amber",
        "diff_add",
        "diff_del",
        "diff_add_word",
        "diff_del_word",
    ):
        assert name in theme.STYLES


def test_jack_theme_is_a_rich_theme_with_those_styles() -> None:
    from rich.theme import Theme

    t = theme.jack_theme()
    assert isinstance(t, Theme)
    assert "assistant" in t.styles
