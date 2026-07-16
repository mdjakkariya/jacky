"""Rendered-screen marker predicates for the E2E harness."""

from __future__ import annotations

from typing import Any

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
    # 'esc to interrupt' now lives in the always-present status bar, so 'working' keys on the
    # spinner glyph in the live region — the status-bar hint must NOT read as working.
    spinner = "⠹ Planning…  ·  esc to interrupt · 3s"
    status_bar_idle = " auto mode  · esc to interrupt · /help · ^C quit \n❯ "
    assert markers.working(spinner)
    assert not markers.working(status_bar_idle)
    assert not markers.working("⏺ Done.\n❯ ")
    # A turn has visibly started on a spinner, a tool line, or a gate — but not at idle.
    assert markers.turn_started(spinner)
    assert markers.turn_started("  ⎿  Read foo.py")
    assert markers.turn_started("approve? [y]es · [n]o")
    assert not markers.turn_started(status_bar_idle)


def test_awaiting_reply_is_the_live_gate_prompt() -> None:
    # The gate affordance is transient (shown only while awaiting), so its presence is the
    # live-gate signal — no stale-card case to disambiguate.
    assert markers.awaiting_reply("Run this command?\n\n  $ mkdir x\napprove? [y]es · [n]o")
    assert markers.awaiting_reply("Approve this plan?\n[y]es · [n]o · or type a change")
    assert not markers.awaiting_reply("⏺ Done.\n❯ ")
    assert not markers.awaiting_reply("⠹ Working…  ·  esc to interrupt · 2s")
    assert "awaiting_reply" in markers.BY_NAME


def test_plan_vs_permission_gate() -> None:
    plan = "Approve this plan?\n[y]es · [n]o · or type a change"
    perm = "Run this command?\n\n  $ mkdir x\napprove? [y]es · [n]o"
    assert markers.plan_card(plan) and not markers.permission_card(plan)
    assert markers.permission_card(perm) and not markers.plan_card(perm)
    assert markers.any_gate(plan) and markers.any_gate(perm)


def test_cost_view_matches_the_rendered_cost_summary() -> None:
    # Render the REAL /cost output (same renderer the TUI uses) and assert the marker fires
    # on it but not on an ordinary reply — so the E2E /cost scenario syncs to the true screen.
    from rich.console import Console

    from autobot.cli import render

    bucket: dict[str, Any] = {
        "turns": 1,
        "in": 52,
        "out": 4079,
        "cache_read": 219067,
        "cache_write": 54908,
        "tokens": 4131,
        "usd": 0.333,
        "has_unpriced": False,
    }
    payload: dict[str, Any] = {
        "ctx": {"model": "claude-sonnet-5"},
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "rollups": {
            "totals": {"today": bucket, "last_7d": bucket, "last_30d": bucket, "all_time": bucket},
            "daily": [],
            "by_model": [{"key": "claude-sonnet-5", **bucket}],
            "by_provider": [],
            "by_workspace": [],
            "session": bucket,
        },
    }
    console = Console(width=90)
    with console.capture() as cap:
        console.print(render.render_cost(payload, 90))
    screen = cap.get()
    assert markers.cost_view(screen)
    assert not markers.cost_view("⏺ Done.\n❯ ")


def test_by_name_maps_all() -> None:
    for name in (
        "reply_present",
        "tool_line",
        "plan_card",
        "permission_card",
        "any_gate",
        "working",
        "turn_started",
        "cost_view",
        "error",
        "idle_prompt",
    ):
        assert name in markers.BY_NAME
