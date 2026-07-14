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
    assert markers.turn_started("(y) yes   (e) edit   (n) no")
    assert not markers.turn_started("⏺ Done.\n❯ ")


def test_awaiting_reply_is_the_live_gate_prompt() -> None:
    # The single-key prompt is transient (erased once answered), so its mere presence is
    # the live-gate signal — no stale-card case to disambiguate.
    assert markers.awaiting_reply("Run this command?\n\n  $ mkdir x\n(y) yes   (n) no")
    assert markers.awaiting_reply("Approve this plan?\n(y) yes   (e) edit   (n) no")
    assert not markers.awaiting_reply("⏺ Done.\n❯ ")
    assert not markers.awaiting_reply("⠹ Working…  ·  esc to interrupt · 2s")
    assert "awaiting_reply" in markers.BY_NAME


def test_plan_vs_permission_gate() -> None:
    plan = "Approve this plan?\n(y) yes   (e) edit   (n) no"
    perm = "Run this command?\n\n  $ mkdir x\n(y) yes   (n) no"
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
