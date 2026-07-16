"""Pure large-paste logic: thresholds, placeholder format, path check, PasteStore round-trip."""

from __future__ import annotations

from pathlib import Path

from autobot.cli import paste


def test_should_collapse_by_lines_or_chars() -> None:
    assert paste.should_collapse("\n".join("x" for _ in range(11)))  # 11 lines
    assert paste.should_collapse("x" * 1000)  # very long single line
    assert not paste.should_collapse("a\nb\nc\nd")  # 4 short lines fit in the growing box
    assert not paste.should_collapse("x" * 300)  # short single line
    assert not paste.should_collapse("short")


def test_summary_lines_vs_chars() -> None:
    assert paste.summary("a\nb\nc") == "3 lines"
    assert paste.summary("x" * 512) == "512 chars"


def test_placeholder_format() -> None:
    assert paste.placeholder(1, "a\nb\nc\nd") == "[Pasted #1 · 4 lines]"
    assert paste.placeholder(2, "x" * 500) == "[Pasted #2 · 500 chars]"


def test_is_existing_path(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    assert paste.is_existing_path("note.txt", str(tmp_path)) == "note.txt"
    assert paste.is_existing_path(str(tmp_path / "note.txt"), str(tmp_path)).endswith("note.txt")  # type: ignore[union-attr]
    assert paste.is_existing_path("nope.txt", str(tmp_path)) is None
    assert paste.is_existing_path("a\nb", str(tmp_path)) is None  # multi-line is not a path
    assert paste.is_existing_path("   ", str(tmp_path)) is None


def test_trailing_placeholder_only_when_at_cursor() -> None:
    tok = "[Pasted #1 · 9 lines]"
    assert paste.trailing_placeholder(f"fix this {tok}") == tok
    assert paste.trailing_placeholder(f"{tok} more text") is None  # not at the end
    assert paste.trailing_placeholder("no placeholder here") is None


def test_paste_store_add_expand_forget() -> None:
    store = paste.PasteStore()
    body = "line1\nline2\nline3\nline4\nline5"
    tok = store.add(body)
    assert tok == "[Pasted #1 · 5 lines]"
    # A message carrying the placeholder expands back to the real content.
    assert store.expand(f"please review {tok} thanks") == f"please review {body} thanks"
    # After forgetting, the token is left literal (no stashed content).
    store.forget(tok)
    assert store.expand(f"x {tok}") == f"x {tok}"


def test_paste_store_ids_increment_and_multiple_expand() -> None:
    store = paste.PasteStore()
    body_a = "a\nb\nc\nd"
    body_b = "x" * 500
    a = store.add(body_a)
    b = store.add(body_b)
    assert a == "[Pasted #1 · 4 lines]" and b == "[Pasted #2 · 500 chars]"
    assert store.expand(f"{a} and {b}") == f"{body_a} and {body_b}"
