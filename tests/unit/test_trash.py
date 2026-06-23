"""Tests for the empty_trash tool (no real Trash, no osascript)."""

from __future__ import annotations

from autobot.core.types import Risk
from autobot.tools.registry import ToolRegistry
from autobot.tools.trash import empty_trash, register_trash_tools


def test_empty_trash_runs_finder_and_reports_success() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "5"  # Finder found (and emptied) 5 items

    msg = empty_trash(runner=runner)
    assert "emptied the Trash" in msg
    assert calls and calls[0][0] == "osascript"


def test_empty_trash_reports_already_empty_without_erroring() -> None:
    # An already-empty Trash returns count 0 (and -128 is avoided by counting first).
    msg = empty_trash(runner=lambda _a: (0, "0"))
    assert "already empty" in msg.lower()
    assert "couldn't" not in msg.lower()


def test_empty_trash_counts_before_emptying() -> None:
    # The script must count items and only empty when there are some — that's what
    # prevents the already-empty -128.
    captured: list[list[str]] = []

    def runner(a: list[str]) -> tuple[int, str]:
        captured.append(a)
        return (0, "0")

    empty_trash(runner=runner)
    script = captured[0][-1]
    assert "count of items in trash" in script
    assert "if _n > 0 then" in script


def test_empty_trash_reports_generic_failure() -> None:
    msg = empty_trash(runner=lambda _a: (1, "boom"))
    assert "couldn't empty" in msg.lower() and "boom" in msg


def test_empty_trash_disables_finders_warning_to_avoid_minus_128() -> None:
    # The script must turn off Finder's "are you sure?" warning (and restore it),
    # so `empty the trash` doesn't pop a dialog and return -128.
    captured: list[list[str]] = []

    def runner(a: list[str]) -> tuple[int, str]:
        captured.append(a)
        return (0, "")

    empty_trash(runner=runner)
    script = captured[0][-1]
    assert "warns before emptying of trash to false" in script
    assert "empty the trash" in script


def test_empty_trash_reports_canceled_cleanly_on_minus_128() -> None:
    # Real Finder error text; the apostrophe in "can't" is U+2019, matching the OS.
    err = (
        "29:44: execution error: Finder got an error: "
        "The operation can’t be completed. (-128)"  # noqa: RUF001
    )
    msg = empty_trash(runner=lambda _a: (1, err))
    assert "canceled" in msg.lower()
    assert "-128" not in msg and "execution error" not in msg  # no raw AppleScript noise


def test_empty_trash_explains_automation_permission_denied() -> None:
    denied = "Not allowed to send Apple events to Finder. (-1743)"
    msg = empty_trash(runner=lambda _a: (1, denied))
    assert "Automation" in msg and "Privacy" in msg


def test_registers_destructive_with_friendly_prompt() -> None:
    reg = ToolRegistry()
    register_trash_tools(reg)
    spec = reg.get("empty_trash")
    assert spec is not None
    assert spec.risk is Risk.DESTRUCTIVE
    assert spec.confirm_prompt and "Trash" in spec.confirm_prompt
