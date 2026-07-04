"""TurnChannel handoff + SuspendingConfirmer routing (no real threads needed)."""

from __future__ import annotations

from autobot.agent.coder_turn import SuspendingConfirmer, TurnChannel


def test_channel_ask_returns_the_preloaded_answer() -> None:
    ch = TurnChannel()
    ch.answer("approve", "")  # preload so ask() returns immediately (single-threaded test)
    got = ch.ask({"status": "plan", "reply": "1. do x"})
    assert got == {"value": "approve", "text": ""}
    assert ch.poll() == {"status": "plan", "reply": "1. do x"}  # the event reached the out queue


def test_channel_done_emits_done_event() -> None:
    ch = TurnChannel()
    ch.done("all set")
    assert ch.poll() == {"status": "done", "reply": "all set"}


def test_confirmer_yes_proceeds_no_cancels() -> None:
    ch = TurnChannel()
    sc = SuspendingConfirmer()
    sc.set_channel(ch)
    ch.answer("yes")
    assert sc.confirm("run pytest?", "danger") is True
    ch.answer("no")
    assert sc.confirm("run pytest?", "danger") is False


def test_confirmer_action_maps_to_once_or_cancel() -> None:
    ch = TurnChannel()
    sc = SuspendingConfirmer()
    sc.set_channel(ch)
    ch.answer("yes")
    assert sc.confirm_action("proceed?", "danger") == "once"
    ch.answer("no")
    assert sc.confirm_action("proceed?", "danger") == ""


def test_confirmer_without_channel_refuses() -> None:
    sc = SuspendingConfirmer()  # no active turn
    assert sc.confirm("anything?") is False
