"""Tests for the pure per-turn PlanState."""

from __future__ import annotations

from autobot.agent.plan_state import PlanState, TodoItem


def test_seed_all_pending() -> None:
    s = PlanState(["a", "b"])
    assert s.items == [TodoItem("a", "pending"), TodoItem("b", "pending")]
    assert s.used() is False and s.all_settled() is False


def test_replace_updates_statuses() -> None:
    s = PlanState(["a", "b"])
    s.replace([{"step": "a", "status": "done"}, {"step": "b", "status": "in_progress"}])
    assert s.used() is True
    assert [(i.step, i.status) for i in s.items] == [("a", "done"), ("b", "in_progress")]
    assert s.pending() == [TodoItem("b", "in_progress")]
    assert s.all_settled() is False
    assert s.summary() == "1/2 done"


def test_all_settled_when_done_or_blocked() -> None:
    s = PlanState(["a", "b"])
    s.replace([{"step": "a", "status": "done"}, {"step": "b", "status": "blocked"}])
    assert s.all_settled() is True


def test_unknown_status_coerced_and_missing_step_skipped() -> None:
    s = PlanState([])
    s.replace([{"step": "x", "status": "weird"}, {"status": "done"}, {"step": "y"}])
    # weird -> pending; the {"status":"done"} with no step is skipped; y with no status -> pending
    assert [(i.step, i.status) for i in s.items] == [("x", "pending"), ("y", "pending")]


def test_empty_replace_is_noop() -> None:
    s = PlanState(["a"])
    s.replace([])
    assert s.items == [TodoItem("a", "pending")] and s.used() is False


def test_remaining_text_lists_open_steps() -> None:
    s = PlanState(["a", "b", "c"])
    s.replace(
        [
            {"step": "a", "status": "done"},
            {"step": "b", "status": "in_progress"},
            {"step": "c", "status": "pending"},
        ]
    )
    assert s.remaining_text() == "- b\n- c"
