"""Tests for the pure search/replace engine (exact match + one whitespace-tolerant pass)."""

from __future__ import annotations

from autobot.tools.code.edits import apply_replace


def test_exact_unique_replace() -> None:
    r = apply_replace("a = 1\nb = 2\n", "b = 2", "b = 3")
    assert r.ok
    assert r.content == "a = 1\nb = 3\n"
    assert "exact" in r.detail.lower()


def test_empty_search_is_rejected() -> None:
    r = apply_replace("x", "", "y")
    assert not r.ok
    assert r.content == "x"
    assert "empty" in r.detail.lower()


def test_identical_find_and_replace_rejected() -> None:
    r = apply_replace("x = 1\n", "x = 1", "x = 1")
    assert not r.ok
    assert r.content == "x = 1\n"
    assert "identical" in r.detail.lower()


def test_not_found_leaves_content_unchanged() -> None:
    r = apply_replace("a = 1\n", "zzz", "q")
    assert not r.ok
    assert r.content == "a = 1\n"
    assert "not found" in r.detail.lower()


def test_multiple_exact_is_ambiguous_by_default() -> None:
    r = apply_replace("x = 1\nx = 1\n", "x = 1", "x = 2")
    assert not r.ok
    assert r.content == "x = 1\nx = 1\n"  # nothing changed
    assert "unique" in r.detail.lower()


def test_replace_all_replaces_every_occurrence() -> None:
    r = apply_replace("x = 1\nx = 1\n", "x = 1", "x = 2", replace_all=True)
    assert r.ok
    assert r.content == "x = 2\nx = 2\n"


def test_trailing_whitespace_drift_matches() -> None:
    # The file has trailing spaces the model can't see in line-numbered output; still matches.
    content = "def f():\n    return 1   \n"
    r = apply_replace(content, "    return 1\n", "    return 2\n")
    assert r.ok
    assert r.content == "def f():\n    return 2\n"
    assert "whitespace" in r.detail.lower()


def test_multiline_exact_block_replace() -> None:
    r = apply_replace("start\nline1\nline2\nend\n", "line1\nline2", "only")
    assert r.ok
    assert r.content == "start\nonly\nend\n"


def test_no_trailing_newline_preserved() -> None:
    r = apply_replace("a\nb", "b", "c")  # file has no final newline
    assert r.ok
    assert r.content == "a\nc"
