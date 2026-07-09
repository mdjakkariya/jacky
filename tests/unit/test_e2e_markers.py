"""Rendered-screen marker predicates for the E2E harness."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e import markers


def test_reply_and_tool_and_idle() -> None:
    assert markers.reply_present("⏺ Done.")
    assert markers.tool_line("  ⎿  Read foo.py")
    assert markers.idle_prompt("⏺ hi\n❯ ")
    assert not markers.idle_prompt("⏺ working…")


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
        "error",
        "idle_prompt",
    ):
        assert name in markers.BY_NAME
