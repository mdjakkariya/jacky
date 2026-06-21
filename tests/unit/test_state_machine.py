"""Tests for the orchestrator state machine and turn flow."""

from __future__ import annotations

import numpy as np
import pytest

from autobot.config import Settings
from autobot.core.types import AudioClip, Risk, State, ToolCall, ToolResult, Transcription
from autobot.orchestrator.state_machine import (
    InvalidTransitionError,
    Orchestrator,
    StateMachine,
)


def test_legal_transition_updates_state_and_notifies() -> None:
    seen: list[tuple[State, State]] = []
    sm = StateMachine(on_change=lambda old, new: seen.append((old, new)))
    sm.transition(State.LISTENING)
    assert sm.state is State.LISTENING
    assert seen == [(State.IDLE, State.LISTENING)]


def test_illegal_transition_raises() -> None:
    sm = StateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition(State.RESPONDING)  # IDLE -> RESPONDING is not allowed


def test_executing_can_repeat_for_multiple_tools() -> None:
    sm = StateMachine(initial=State.PLANNING)
    sm.transition(State.EXECUTING)
    sm.transition(State.EXECUTING)  # second tool in the same turn
    assert sm.state is State.EXECUTING


# --- Orchestrator turn flow (with fakes, no models or mic) ----------------


class _FakeAudio:
    def record_clip(self) -> AudioClip:
        return np.zeros(4, dtype=np.float32)


class _FakeSTT:
    def __init__(self, text: str) -> None:
        self._text = text

    def transcribe(self, _audio: AudioClip) -> Transcription:
        return Transcription(text=self._text, confidence=0.9)


class _ToolingLLM:
    """An LLM stub that always asks to run one tool, via the executor."""

    def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
        result = execute(ToolCall(name="create_file", arguments={"path": "x"}))
        return f"done: {result.content}"


class _RecordingGate:
    def __init__(self, risk: Risk = Risk.WRITE) -> None:
        self.calls: list[ToolCall] = []
        self._risk = risk

    def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(name=call.name, content="ok", ok=True)

    def risk_of(self, _name: str) -> Risk:
        return self._risk


class _RecordingTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


def _orchestrator(text: str, gate: object, tts: object | None = None) -> Orchestrator:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    transitions: list[State] = []
    orch = Orchestrator(
        settings=Settings(),
        audio=_FakeAudio(),
        stt=_FakeSTT(text),
        llm=_ToolingLLM(),
        gate=gate,  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=tts or NullTTS(),  # type: ignore[arg-type]
        on_state=lambda _old, new: transitions.append(new),
    )
    orch._transitions = transitions  # type: ignore[attr-defined]
    return orch


def test_turn_with_tool_walks_through_executing_and_back_to_idle() -> None:
    gate = _RecordingGate()
    orch = _orchestrator("create a file", gate)
    orch.run_once()
    seen = orch._transitions  # type: ignore[attr-defined]
    assert seen == [
        State.LISTENING,
        State.TRANSCRIBING,
        State.PLANNING,
        State.EXECUTING,
        State.RESPONDING,
        State.IDLE,
    ]
    assert gate.calls and gate.calls[0].name == "create_file"
    assert orch.state is State.IDLE


def test_reply_is_spoken_via_tts() -> None:
    tts = _RecordingTTS()
    orch = _orchestrator("create a file", _RecordingGate(), tts)
    orch.run_once()
    # The final reply is spoken (an acknowledgement may precede it).
    assert tts.spoken[-1] == "done: ok"


def test_acknowledgement_matches_action_tool() -> None:
    from autobot.orchestrator.state_machine import _CONFIRMING_ACKS

    tts = _RecordingTTS()
    # create_file is a WRITE -> a "confirming intent" ack, spoken before the reply.
    orch = _orchestrator("create a file", _RecordingGate(risk=Risk.WRITE), tts)
    orch.run_once()
    assert tts.spoken[0] in _CONFIRMING_ACKS
    assert len(tts.spoken) == 2


def test_acknowledgement_matches_lookup_tool() -> None:
    from autobot.orchestrator.state_machine import _NEUTRAL_ACKS

    tts = _RecordingTTS()
    # A READ_ONLY tool -> a neutral "checking" ack.
    orch = _orchestrator("look something up", _RecordingGate(risk=Risk.READ_ONLY), tts)
    orch.run_once()
    assert tts.spoken[0] in _NEUTRAL_ACKS


def test_empty_transcription_returns_to_idle_without_planning() -> None:
    gate = _RecordingGate()
    orch = _orchestrator("", gate)
    orch.run_once()
    seen = orch._transitions  # type: ignore[attr-defined]
    assert seen == [State.LISTENING, State.TRANSCRIBING, State.IDLE]
    assert not gate.calls
    assert orch.state is State.IDLE


def test_looks_incomplete_detects_cut_off_phrases() -> None:
    from autobot.orchestrator.state_machine import _looks_incomplete

    # Cut off mid-thought: no terminal punctuation, ends on a connective word.
    assert _looks_incomplete("send a message to") is True
    assert _looks_incomplete("search on google about got where how and") is True
    assert _looks_incomplete("tell me about the") is True
    # Complete: terminal punctuation, or ends on a content word.
    assert _looks_incomplete("what time is it?") is False
    assert _looks_incomplete("open spotify") is False
    assert _looks_incomplete("turn off the lights.") is False
    # Too short / empty -> not treated as incomplete.
    assert _looks_incomplete("and") is False
    assert _looks_incomplete("   ") is False


class _ContinuingAudio:
    """Fake audio that returns a first clip, then a continuation on re-open."""

    def __init__(self) -> None:
        self.continued = False
        self.last_speech_started_at = 1.0

    def record_clip(self) -> AudioClip:
        return np.ones(4, dtype=np.float32)

    def record_continuation(self, max_wait_s: float = 2.0) -> AudioClip:
        self.continued = True
        return np.ones(4, dtype=np.float32)


class _GrowingSTT:
    """Returns an incomplete transcript first, then a complete one after re-open."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, _audio: AudioClip) -> Transcription:
        self.calls += 1
        text = "remind me to" if self.calls == 1 else "remind me to call mom"
        return Transcription(text=text, confidence=0.9)


def test_incomplete_utterance_triggers_reopen_and_retranscribe() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    audio = _ContinuingAudio()
    stt = _GrowingSTT()
    orch = Orchestrator(
        settings=Settings(reopen_on_incomplete=True),
        audio=audio,  # type: ignore[arg-type]
        stt=stt,  # type: ignore[arg-type]
        llm=_EchoLLM(),
        gate=_RecordingGate(),
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )
    orch.run_once()
    assert audio.continued is True  # re-opened because "remind me to" looked cut off
    assert stt.calls == 2  # transcribed again on the combined audio


class _EchoLLM:
    def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
        return user_text


def test_ack_phrase_maps_risk_to_the_right_pool() -> None:
    from autobot.orchestrator.state_machine import (
        _CONFIRMING_ACKS,
        _NEUTRAL_ACKS,
        _THINKING_ACKS,
        _ack_phrase,
    )

    assert _ack_phrase(Risk.READ_ONLY) in _NEUTRAL_ACKS
    assert _ack_phrase(Risk.WRITE) in _CONFIRMING_ACKS
    assert _ack_phrase(Risk.DESTRUCTIVE) in _CONFIRMING_ACKS
    assert _ack_phrase(None) in _THINKING_ACKS  # unknown tool -> thinking
