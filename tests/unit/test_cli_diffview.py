"""Unified-diff parsing (line numbers + kinds) and full-width rich rendering."""

from __future__ import annotations

from rich.text import Text

from autobot.cli import diffview

_DIFF = """\
diff --git a/api.py b/api.py
--- a/api.py
+++ b/api.py
@@ -14,2 +14,3 @@ def fetch(url):
 def fetch(url):
-    return _get(url)
+    return _get(url, timeout=5)
+    # retry later
"""


def test_parse_diff_kinds_and_line_numbers() -> None:
    rows = diffview.parse_diff(_DIFF)
    kinds = [r.kind for r in rows]
    assert "file" in kinds and "hunk" in kinds
    add = [r for r in rows if r.kind == "add"]
    dele = [r for r in rows if r.kind == "del"]
    assert any("timeout=5" in r.text for r in add)
    assert any("_get(url)" in r.text for r in dele)
    # context line carries its new-file line number from the @@ header (14)
    ctx = [r for r in rows if r.kind == "ctx"]
    assert ctx and ctx[0].line_no == 14


def test_render_diff_returns_a_renderable_with_the_change() -> None:
    from rich.console import Console

    from autobot.cli.theme import jack_theme

    console = Console(record=True, width=80, theme=jack_theme(), force_terminal=True)
    console.print(diffview.render_diff(_DIFF, width=72))
    out = console.export_text()
    assert "timeout=5" in out and "api.py" in out


def test_word_highlight_applied_to_paired_del_add_lines() -> None:
    """A same-length word substitution highlights only the changed word on each side."""
    diff = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
-foo = 1
+foo = 2
"""
    lines = [r for r in diffview.render_diff(diff, width=40).renderables if isinstance(r, Text)]
    del_line = next(t for t in lines if "foo = 1" in t.plain)
    add_line = next(t for t in lines if "foo = 2" in t.plain)

    assert any(style == "diff_del_word" for _, _, style in del_line.spans)
    assert any(style == "diff_add_word" for _, _, style in add_line.spans)

    # the highlighted span on each side covers exactly the changed digit.
    del_idx = del_line.plain.index("1")
    add_idx = add_line.plain.index("2")
    assert any(
        style == "diff_del_word" and start <= del_idx < end for start, end, style in del_line.spans
    )
    assert any(
        style == "diff_add_word" and start <= add_idx < end for start, end, style in add_line.spans
    )
