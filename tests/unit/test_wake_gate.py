"""Tests for text-level wake gating (transcribe-then-match)."""

from __future__ import annotations

from autobot.orchestrator.wake_gate import (
    Address,
    PassThroughGate,
    SttWakeGate,
    extract_command,
)


def test_extract_command_strips_leading_wake_phrase() -> None:
    assert extract_command("hey jarvis what's the time", "jarvis") == "what's the time"
    assert extract_command("Jarvis, turn on the lights", "jarvis") == "turn on the lights"


def test_extract_command_wake_word_only_returns_empty_string() -> None:
    assert extract_command("hey jarvis", "jarvis") == ""
    assert extract_command("jarvis", "jarvis") == ""


def test_extract_command_no_wake_word_returns_none() -> None:
    assert extract_command("turn on the lights", "jarvis") is None
    # Too far into the sentence to be an address.
    assert extract_command("so anyway i told jarvis to wait", "jarvis") is None


def test_extract_command_matches_last_token_of_multiword_phrase() -> None:
    assert extract_command("hey jarvis play music", "hey jarvis") == "play music"


def test_passthrough_gate_treats_everything_as_command() -> None:
    gate = PassThroughGate()
    result = gate.process("  whatever i say  ")
    assert result.address is Address.COMMAND
    assert result.command == "whatever i say"


def test_stt_gate_requires_wake_word_when_not_in_follow_up() -> None:
    gate = SttWakeGate("jarvis", follow_up_window_s=8.0, clock=lambda: 0.0)
    assert gate.process("turn on the lights").address is Address.IGNORED
    res = gate.process("jarvis what's the time")
    assert res.address is Address.COMMAND and res.command == "what's the time"
    assert gate.process("hey jarvis").address is Address.GREETED


def test_stt_gate_follow_up_window_accepts_without_wake_word() -> None:
    now = {"t": 0.0}
    gate = SttWakeGate("jarvis", follow_up_window_s=8.0, clock=lambda: now["t"])

    first = gate.process("jarvis what time is it")
    assert first.address is Address.COMMAND
    gate.mark_turn_complete()  # opens the follow-up window

    now["t"] = 3.0  # within the 8s window
    follow = gate.process("and what's the date")
    assert follow.address is Address.COMMAND and follow.command == "and what's the date"


def test_stt_gate_follow_up_window_lapses() -> None:
    now = {"t": 0.0}
    gate = SttWakeGate("jarvis", follow_up_window_s=8.0, clock=lambda: now["t"])
    gate.process("jarvis hello")
    gate.mark_turn_complete()

    now["t"] = 20.0  # well past the window
    assert gate.process("turn on the lights").address is Address.IGNORED


def test_follow_up_measured_from_speech_start_not_processing_time() -> None:
    # The bug: a reply that *began* inside the window but finished after it (long
    # phrase + slow STT) was dropped. With started_at we judge from speech start.
    now = {"t": 0.0}
    gate = SttWakeGate("jarvis", follow_up_window_s=30.0, clock=lambda: now["t"])
    gate.process("jarvis hello")
    gate.mark_turn_complete()  # window opens at t=0

    now["t"] = 40.0  # it's now 40s later (capture + transcription took a while)
    # Started speaking at t=20 (within the 30s window): accept it.
    accepted = gate.process("tell me about the weather", started_at=20.0)
    assert accepted.address is Address.COMMAND
    # Without a speech-start time, "now" (40s) is past the window -> ignored.
    assert gate.process("tell me about the weather").address is Address.IGNORED


def test_stt_gate_end_follow_up_requires_wake_word_again() -> None:
    # After a dismiss, end_follow_up() must close the window so the very next
    # utterance (even if immediate) is ignored unless it has the wake word.
    now = {"t": 0.0}
    gate = SttWakeGate("jarvis", follow_up_window_s=30.0, clock=lambda: now["t"])
    gate.process("jarvis go away")
    gate.end_follow_up()  # dismissed — don't keep listening

    now["t"] = 2.0  # well within what would have been the follow-up window
    assert gate.process("actually do this").address is Address.IGNORED
    # The wake word still brings it back.
    assert gate.process("jarvis what time is it").address is Address.COMMAND
