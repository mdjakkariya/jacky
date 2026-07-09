"""Rendered-screen marker predicates for the E2E harness."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e import markers


def test_reply_and_tool_and_idle() -> None:
    assert markers.reply_present("⏺ Done.")
    assert markers.tool_line("  ⎿  Read foo.py")
    assert markers.idle_prompt("⏺ hi\n❯ ")
    assert markers.idle_prompt("⏺ hi\n❯")
    assert not markers.idle_prompt("⏺ working…")
    # A prompt holding un-submitted input is NOT idle — the turn hasn't run yet.
    assert not markers.idle_prompt("⏺ hi\n❯ do a thing")


def test_working_and_turn_started() -> None:
    spinner = "⠹ Planning…  ·  esc to interrupt · 3s"
    assert markers.working(spinner)
    assert not markers.working("⏺ Done.\n❯ ")
    # A turn has visibly started on a spinner, a tool line, or a gate — but not at idle.
    assert markers.turn_started(spinner)
    assert markers.turn_started("  ⎿  Read foo.py")
    assert markers.turn_started("Proceed?   [1] Yes   [2] Edit")
    assert not markers.turn_started("⏺ Done.\n❯ ")


def test_plan_vs_permission_gate() -> None:
    plan = "Here's my plan\nProceed?   [1] Yes   [2] Edit   [3] No\n> "
    perm = "Run `pytest`?\nProceed?   [1] Yes, run it   [2] No\n> "
    assert markers.plan_card(plan) and not markers.permission_card(plan)
    assert markers.permission_card(perm) and not markers.plan_card(perm)
    assert markers.any_gate(plan) and markers.any_gate(perm)


def test_by_name_maps_all() -> None:
    for name in (
        "reply_present",
        "tool_line",
        "plan_card",
        "permission_card",
        "any_gate",
        "working",
        "turn_started",
        "error",
        "idle_prompt",
    ):
        assert name in markers.BY_NAME
