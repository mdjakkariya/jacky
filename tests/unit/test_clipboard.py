"""Tests for the clipboard tools (pbpaste/pbcopy via an injected runner)."""

from __future__ import annotations

from autobot.tools.clipboard import read_clipboard, set_clipboard


def test_read_clipboard_returns_contents() -> None:
    out = read_clipboard(runner=lambda _a, _s: (0, "hello world"))
    assert "hello world" in out


def test_read_clipboard_empty_is_friendly() -> None:
    assert "empty" in read_clipboard(runner=lambda _a, _s: (0, "   ")).lower()


def test_read_clipboard_caps_huge_content() -> None:
    big = "x" * 50_000
    out = read_clipboard(runner=lambda _a, _s: (0, big))
    assert "…" in out and len(out) < len(big)  # truncated


def test_read_clipboard_reports_error() -> None:
    out = read_clipboard(runner=lambda _a, _s: (1, "boom"))
    assert "couldn't read" in out.lower() and "boom" in out


def test_set_clipboard_pipes_text_via_stdin() -> None:
    seen: list[tuple[list[str], str | None]] = []

    def fake(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
        seen.append((argv, stdin))
        return 0, ""

    out = set_clipboard("copy me", runner=fake)
    assert seen[0][0] == ["pbcopy"] and seen[0][1] == "copy me"
    assert "7 characters" in out


def test_set_clipboard_singular_grammar() -> None:
    out = set_clipboard("x", runner=lambda _a, _s: (0, ""))
    assert "1 character)" in out


def test_set_clipboard_reports_error() -> None:
    out = set_clipboard("x", runner=lambda _a, _s: (1, "nope"))
    assert "couldn't set" in out.lower()
