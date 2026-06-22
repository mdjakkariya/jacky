"""Tests for the central permission tracking (autobot.permissions)."""

from __future__ import annotations

import autobot.permissions as perms


def test_snapshot_lists_the_three_permissions() -> None:
    snap = perms.snapshot()
    keys = {row["key"] for row in snap}
    assert keys == {perms.MICROPHONE, perms.ACCESSIBILITY, perms.AUTOMATION}
    for row in snap:
        assert set(row) == {"key", "label", "description", "status"}
        assert row["status"] in {perms.GRANTED, perms.NEEDED, perms.UNKNOWN}


def test_observed_denial_wins_over_unknown_native() -> None:
    # Off-macOS the native checks return UNKNOWN; an observed denial should surface.
    perms._observed.clear()
    perms.note_observed(perms.AUTOMATION, granted=False)
    assert perms.status_of(perms.AUTOMATION) == perms.NEEDED
    perms.note_observed(perms.AUTOMATION, granted=True)
    assert perms.status_of(perms.AUTOMATION) == perms.GRANTED
    perms._observed.clear()


def test_needed_message_names_the_permission() -> None:
    msg = perms.needed_message(perms.AUTOMATION)
    assert "Automation" in msg
    assert "ask me again" in msg.lower()


def test_open_pane_unknown_key_is_noop() -> None:
    assert perms.open_pane("not-a-real-key") is False
