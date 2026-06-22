"""Tests for self-echo rejection (barge-in capturing Jack's own voice)."""

from __future__ import annotations

from autobot.orchestrator.state_machine import _is_self_echo


def test_verbatim_fragment_of_reply_is_echo() -> None:
    assert _is_self_echo("Sure thing.", "Sure thing! Opening YouTube for you.") is True


def test_action_phrase_echo() -> None:
    assert _is_self_echo("opening youtube", "Opening YouTube for you now.") is True


def test_real_command_is_not_echo() -> None:
    # New words the reply didn't contain — a genuine interruption.
    assert _is_self_echo("can you open chrome", "Sure, opening Safari now.") is False


def test_empty_inputs_are_not_echo() -> None:
    assert _is_self_echo("", "anything") is False
    assert _is_self_echo("something", "") is False


def test_high_word_overlap_counts_as_echo() -> None:
    assert _is_self_echo("opening safari for you", "Opening Safari for you.") is True
