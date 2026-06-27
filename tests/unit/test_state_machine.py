"""Tests for the orchestrator state machine and turn flow."""

from __future__ import annotations

import itertools

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
        self.stopped = False

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stopped = True


def _orchestrator(text: str, gate: object, tts: object | None = None) -> Orchestrator:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    transitions: list[State] = []
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),  # exercising the voice loop
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


def test_turn_sets_delivery_mode_voice_then_chat() -> None:
    # The orchestrator tells the LLM how each reply is delivered: spoken on a voice
    # turn, text on a typed turn — so replies are styled for their medium.
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    class _DeliveryLLM:
        def __init__(self) -> None:
            self.modes: list[str] = []

        def set_delivery_mode(self, mode: str) -> None:
            self.modes.append(mode)

        def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
            return "ok"

    llm = _DeliveryLLM()
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("hello"),
        llm=llm,
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )
    orch.run_once()  # a voice turn
    orch.run_text_turn("hi there")  # a typed turn
    assert llm.modes == ["voice", "chat"]


def test_run_tool_executes_through_gate_without_llm() -> None:
    # A clicked action card calls run_tool: the named tool goes straight through the
    # gate (recorded here), returning its result text — no LLM, no state churn.
    gate = _RecordingGate()
    orch = _orchestrator("anything", gate)
    out = orch.run_tool("open_path", {"path": "~/a.pdf"})
    assert out == "ok"
    assert gate.calls == [ToolCall(name="open_path", arguments={"path": "~/a.pdf"})]


def test_text_turn_returns_reply_runs_tool_and_stays_silent() -> None:
    tts = _RecordingTTS()
    gate = _RecordingGate()
    orch = _orchestrator("unused", gate, tts)
    reply = orch.run_text_turn("create a file please")
    assert reply == "done: ok"
    assert gate.calls and gate.calls[0].name == "create_file"  # tools still run
    assert tts.spoken == []  # chat mode: nothing spoken (no ack, no TTS reply)
    assert orch._transitions == [  # type: ignore[attr-defined]
        State.PLANNING,  # forced "thinking" (no mic/listening for a typed turn)
        State.EXECUTING,
        State.RESPONDING,
        State.IDLE,
    ]
    assert orch.state is State.IDLE


def test_chat_turn_emits_context_usage() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    usage = {"used": 12_000, "window": 200_000, "model": "m", "cache_read": 9_000, "cache_write": 0}

    class _UsageLLM(_EchoLLM):
        def context_usage(self) -> dict[str, object]:
            return usage

    seen: list[dict[str, object]] = []
    orch = Orchestrator(
        settings=Settings(),
        audio=_FakeAudio(),
        stt=_FakeSTT("unused"),
        llm=_UsageLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        on_context=seen.append,
    )
    orch.run_text_turn("hello")
    assert seen == [usage]


def test_new_chat_session_resets_llm_and_state() -> None:
    from autobot.core.types import State
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    class _ResettableLLM(_EchoLLM):
        def __init__(self) -> None:
            self.reset_calls = 0

        def new_session(self) -> None:
            self.reset_calls += 1

    llm = _ResettableLLM()
    orch = Orchestrator(
        settings=Settings(),
        audio=_FakeAudio(),
        stt=_FakeSTT("unused"),
        llm=llm,
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )
    orch.run_text_turn("hello")
    orch.new_chat_session()
    assert llm.reset_calls == 1  # the LLM's history was wiped
    assert orch.state is State.IDLE  # the machine rests after a reset


def test_new_chat_session_is_safe_without_llm_support() -> None:
    # _EchoLLM has no new_session(); the orchestrator must no-op, not crash.
    orch = _orchestrator("unused", _RecordingGate(), _RecordingTTS())
    orch.new_chat_session()  # should not raise


def test_text_turn_ignores_blank_input() -> None:
    orch = _orchestrator("unused", _RecordingGate(), _RecordingTTS())
    assert orch.run_text_turn("   ") == ""


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


def test_is_nonspeech_rejects_noise_artifacts() -> None:
    from autobot.orchestrator.state_machine import _is_nonspeech

    # Whisper non-speech annotations from background noise -> not real speech.
    assert _is_nonspeech("(water splashing)") is True
    assert _is_nonspeech("[music playing]") is True
    assert _is_nonspeech("*sigh*") is True
    assert _is_nonspeech("   ") is True
    assert _is_nonspeech("...") is True
    assert _is_nonspeech("♪♪") is True
    # Real speech (even short) is kept.
    assert _is_nonspeech("open spotify") is False
    assert _is_nonspeech("yes") is False
    assert _is_nonspeech("what's the time?") is False


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
        settings=Settings(reopen_on_incomplete=True, interaction_mode="voice"),
        audio=audio,
        stt=stt,
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )
    orch.run_once()
    assert audio.continued is True  # re-opened because "remind me to" looked cut off
    assert stt.calls == 2  # transcribed again on the combined audio


class _EchoLLM:
    def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
        return user_text


def test_awake_true_after_addressed_turn_false_when_ignored() -> None:
    from autobot.orchestrator.wake_gate import Address, PassThroughGate, WakeResult
    from autobot.tts.null_tts import NullTTS

    class _AwakeAudio:
        def __init__(self) -> None:
            self.awake: list[bool] = []
            self.last_speech_started_at = None

        def record_clip(self) -> AudioClip:
            return np.ones(4, dtype=np.float32)

        def set_awake(self, awake: bool) -> None:
            self.awake.append(awake)

    class _IgnoringGate:
        def process(self, text: str, started_at: float | None = None) -> WakeResult:
            return WakeResult(Address.IGNORED)

        def mark_turn_complete(self) -> None: ...
        def end_follow_up(self) -> None: ...

    # Addressed turn (PassThrough -> COMMAND): we end up awake for the follow-up.
    addressed = _AwakeAudio()
    Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=addressed,
        stt=_FakeSTT("open spotify"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    ).run_once()
    assert addressed.awake[-1] is True

    # Ignored (not addressed): we are not awake, so the orb can rest.
    ignored = _AwakeAudio()
    Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=ignored,
        stt=_FakeSTT("just chatting nearby"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=_IgnoringGate(),
        tts=NullTTS(),
    ).run_once()
    assert ignored.awake[-1] is False


def test_voice_wake_reshows_orb_only_when_addressed() -> None:
    # A voice turn addressed to Jack must re-show the orb (it may have been hidden
    # via the global shortcut/tray); an unaddressed turn must not.
    from autobot.orchestrator.wake_gate import Address, PassThroughGate, WakeResult
    from autobot.tts.null_tts import NullTTS

    class _IgnoringGate:
        def process(self, text: str, started_at: float | None = None) -> WakeResult:
            return WakeResult(Address.IGNORED)

        def mark_turn_complete(self) -> None: ...
        def end_follow_up(self) -> None: ...

    shown: list[bool] = []
    Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("open spotify"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        on_show=lambda: shown.append(True),
    ).run_once()
    assert shown == [True]  # addressed -> orb re-shown

    shown_ignored: list[bool] = []
    Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("just chatting nearby"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=_IgnoringGate(),
        tts=NullTTS(),
        on_show=lambda: shown_ignored.append(True),
    ).run_once()
    assert shown_ignored == []  # not addressed -> orb left as-is


class _StoppableTTS:
    def __init__(self) -> None:
        self.spoke: list[str] = []
        self.stopped = False

    def speak(self, text: str) -> None:
        self.spoke.append(text)

    def stop(self) -> None:
        self.stopped = True


def test_barge_in_stops_reply_and_queues_the_interrupting_utterance() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate

    class _BargeAudio:
        aec_active = True

        def __init__(self) -> None:
            self.last_speech_started_at = 2.0
            self.awake: list[bool] = []

        def record_clip(self) -> AudioClip:
            return np.ones(4, dtype=np.float32)

        def set_awake(self, awake: bool) -> None:
            self.awake.append(awake)

        def monitor_barge_in(
            self, _should_continue: object, on_speech_start: object = None
        ) -> AudioClip:
            if callable(on_speech_start):
                on_speech_start()  # the user started speaking -> stop playback now
            return np.ones(8, dtype=np.float32)  # the user talked over the reply

    audio, tts = _BargeAudio(), _StoppableTTS()
    orch = Orchestrator(
        settings=Settings(barge_in=True, interaction_mode="voice"),
        audio=audio,
        stt=_FakeSTT("open spotify"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=tts,
    )
    orch.run_once()
    assert tts.stopped is True
    assert orch._pending_audio is not None  # queued for the next turn


def test_no_barge_in_when_input_is_not_echo_cancelled() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate

    class _PlainAudio:
        last_speech_started_at = None  # no aec_active attr -> barge-in disabled

        def record_clip(self) -> AudioClip:
            return np.ones(4, dtype=np.float32)

        def monitor_barge_in(
            self, _should_continue: object, on_speech_start: object = None
        ) -> AudioClip:
            raise AssertionError("must not monitor without AEC")

    tts = _StoppableTTS()
    orch = Orchestrator(
        settings=Settings(barge_in=True, interaction_mode="voice"),
        audio=_PlainAudio(),
        stt=_FakeSTT("open spotify"),
        llm=_EchoLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=tts,
    )
    orch.run_once()
    assert tts.spoke == ["open spotify"]  # spoken directly, never monitored
    assert orch._pending_audio is None


def test_voice_and_chat_turns_do_not_interleave_state() -> None:
    """A chat turn arriving mid voice-turn must wait, not corrupt the state machine.

    Regression for the cross-thread crashes ("listening -> executing",
    "planning -> transcribing"): the voice loop and a chat turn share one state
    machine, so they must run one at a time under the turn lock.
    """
    import threading

    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    entered = threading.Event()  # the voice turn is parked inside the LLM call
    release = threading.Event()  # let the voice turn finish

    class _BlockingLLM:
        def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
            execute(ToolCall(name="create_file", arguments={"path": "x"}))
            entered.set()
            release.wait(timeout=5)
            return user_text

    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("open spotify"),
        llm=_BlockingLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )

    errors: list[BaseException] = []

    def voice() -> None:
        try:
            orch.run_once()
        except BaseException as exc:
            errors.append(exc)

    chat_reply: list[str] = []

    def chat() -> None:
        try:
            chat_reply.append(orch.run_text_turn("hello from chat"))
        except BaseException as exc:
            errors.append(exc)

    vt = threading.Thread(target=voice)
    vt.start()
    assert entered.wait(timeout=5), "voice turn never reached the LLM call"

    # The voice turn now holds the turn lock; a chat turn must block, not interleave.
    ct = threading.Thread(target=chat)
    ct.start()
    ct.join(timeout=0.3)
    assert ct.is_alive(), "chat turn ran while a voice turn held the lock"

    release.set()  # let the voice turn finish; the chat turn then proceeds
    vt.join(timeout=5)
    ct.join(timeout=5)

    assert not errors, f"turns crashed: {errors}"
    assert chat_reply == ["hello from chat"]
    assert orch.state is State.IDLE


class _AckGate(_RecordingGate):
    """A gate that also exposes per-tool ack hints (like the real PermissionGate)."""

    def __init__(self, acks: dict[str, str]) -> None:
        super().__init__()
        self._acks = acks

    def ack_of(self, name: str) -> str | None:
        return self._acks.get(name)


def test_format_ack_fills_or_drops_the_target() -> None:
    from autobot.orchestrator.state_machine import _format_ack

    assert _format_ack("Opening {target}.", {"name": "Spotify"}) == "Opening Spotify."
    assert _format_ack("Opening {target}.", {}) == "Opening that."  # no arg -> generic
    assert _format_ack("Emptying the Trash.", {}) == "Emptying the Trash."  # no placeholder


def test_ack_uses_the_tools_own_phrase_with_the_argument() -> None:
    tts = _RecordingTTS()
    gate = _AckGate({"create_file": "Opening {target}."})
    orch = _orchestrator("create a file", gate, tts)
    orch.run_once()
    assert "Opening x." in tts.spoken  # the tool's ack, filled with its path arg


def test_silent_ack_speaks_no_filler() -> None:
    tts = _RecordingTTS()
    gate = _AckGate({"create_file": ""})  # explicitly silent, like dismiss
    orch = _orchestrator("create a file", gate, tts)
    orch.run_once()
    assert tts.spoken == ["done: ok"]  # only the reply — no filler ack before it


class _DismissLLM:
    """Runs the dismiss tool, then returns no text (as the model sometimes does)."""

    def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
        execute(ToolCall(name="dismiss", arguments={}))
        return ""


def _chat_orch(llm: object) -> Orchestrator:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    return Orchestrator(
        settings=Settings(),
        audio=_FakeAudio(),
        stt=_FakeSTT("unused"),
        llm=llm,  # type: ignore[arg-type]
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )


def test_chat_turn_never_returns_empty_reply() -> None:
    from autobot.orchestrator.state_machine import _GOODBYES

    # dismiss with no trailing text -> a (random) warm goodbye, not an empty bubble.
    assert _chat_orch(_DismissLLM()).run_text_turn("go away") in _GOODBYES

    class _SilentLLM:
        def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
            return ""

    # A non-dismiss empty reply -> a neutral acknowledgement.
    assert _chat_orch(_SilentLLM()).run_text_turn("hi") == "Done. ✅"


def test_typing_forces_chat_mode_and_silences_voice() -> None:
    # Regression: chat + voice ran at once because the engine stayed in voice mode
    # while the drawer was open. Typing must force chat mode and cut off any spoken
    # reply, so the mic loop goes quiet immediately — independent of the UI's POST.
    tts = _RecordingTTS()
    orch = _orchestrator("unused", _RecordingGate(), tts)
    orch._mode = "voice"  # as if the open-mode POST was missed
    orch.run_text_turn("hey")
    assert orch._mode == "chat"  # forced by the act of typing
    assert tts.stopped is True  # any in-progress voice reply was interrupted


def test_mode_toggle_does_not_reload_models(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    calls = {"llm": 0, "stt": 0}

    class _ReloadLLM(_ToolingLLM):
        def mark_dirty(self) -> None:
            calls["llm"] += 1

    class _ReloadSTT(_FakeSTT):
        def mark_dirty(self) -> None:
            calls["stt"] += 1

    orch = Orchestrator(
        settings=Settings(),
        audio=_FakeAudio(),
        stt=_ReloadSTT("x"),
        llm=_ReloadLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
    )

    # Mode-only change (drawer open/close): switch the mode, reload nothing.
    monkeypatch.setattr(Settings, "load", lambda: Settings(interaction_mode="chat"))
    orch.mark_settings_changed()
    assert calls == {"llm": 0, "stt": 0}
    assert orch._mode == "chat"

    # A real model change reloads just the LLM.
    monkeypatch.setattr(
        Settings, "load", lambda: Settings(interaction_mode="chat", anthropic_model="claude-x")
    )
    orch.mark_settings_changed()
    assert calls == {"llm": 1, "stt": 0}


def test_switch_to_chat_debounces_voice_io_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice→chat arms a *debounced* mic release; a quick switch back cancels it.

    This avoids tearing down + rebuilding the audio engine on a fast chat⇄voice toggle
    (which churns macOS Voice-Processing and can drop AEC). The release only happens
    if we're still in chat when the timer fires.
    """
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    released = {"n": 0}
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT(""),
        llm=_ToolingLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        release_voice_io=lambda: released.__setitem__("n", released["n"] + 1),
    )

    # voice → chat: release is DEBOUNCED (a timer is armed), not immediate.
    monkeypatch.setattr(Settings, "load", lambda: Settings(interaction_mode="chat"))
    orch.mark_settings_changed()
    assert released["n"] == 0
    assert orch._voice_io_release_timer is not None

    # a quick switch back to voice cancels the pending release (mic kept alive).
    monkeypatch.setattr(Settings, "load", lambda: Settings(interaction_mode="voice"))
    orch.mark_settings_changed()
    assert orch._voice_io_release_timer is None
    assert released["n"] == 0

    # when the debounce fires and we're still in chat, the mic is released.
    orch._mode = "chat"
    orch._release_if_still_chat()
    assert released["n"] == 1

    # but if voice resumed by the time it fires, it does NOT release.
    orch._mode = "voice"
    orch._release_if_still_chat()
    assert released["n"] == 1


def test_llm_unavailable_message_is_specific_then_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _orchestrator("hi", _RecordingGate())

    # Local backend down → an actionable Ollama message.
    monkeypatch.setattr(Settings, "load", lambda: Settings(llm_provider="ollama"))
    local = orch._llm_unavailable_message(ConnectionError("refused"))
    assert local is not None and "Ollama" in local

    # Cloud backend down → a connection message (no Ollama mention).
    monkeypatch.setattr(Settings, "load", lambda: Settings(llm_provider="anthropic"))
    cloud = orch._llm_unavailable_message(ConnectionError("refused"))
    assert cloud is not None and "Ollama" not in cloud

    # Anything that isn't a connection failure falls through to the generic handler.
    assert orch._llm_unavailable_message(ValueError("bad")) is None


def test_goodbye_is_a_warm_signoff_and_emoji_free() -> None:
    import re

    from autobot.orchestrator.state_machine import _GOODBYES, _goodbye

    assert _goodbye() in _GOODBYES
    assert all(line.strip() for line in _GOODBYES)
    # Emoji-free so it reads cleanly when spoken (punctuation like — is fine).
    emoji = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf]")
    assert not any(emoji.search(line) for line in _GOODBYES)


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


def test_chat_turn_emits_running_then_done_step() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    steps: list[tuple[int, str, str, str]] = []
    orch = Orchestrator(
        settings=Settings(interaction_mode="chat"),
        audio=_FakeAudio(),
        stt=_FakeSTT("create a file"),
        llm=_ToolingLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        on_step=lambda i, tool, label, status: steps.append((i, tool, label, status)),
    )
    orch.run_text_turn("create a file")
    # A typed (chat) turn drives the chat-drawer trace: running step then done, same index.
    assert (0, "create_file", "Create file", "running") in steps
    assert (0, "create_file", "Create file", "done") in steps


def test_voice_turn_does_not_emit_steps_to_chat_trace() -> None:
    # The step trace is a chat-drawer surface; a voice turn surfaces via spoken cues,
    # so it must NOT emit step events — otherwise a voice turn's trace lingers in the
    # chat drawer (nothing calls clearSteps for a voice turn) and stale rows pile up.
    from autobot.orchestrator.wake_gate import PassThroughGate
    from autobot.tts.null_tts import NullTTS

    steps: list[tuple[int, str, str, str]] = []
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice"),
        audio=_FakeAudio(),
        stt=_FakeSTT("create a file"),
        llm=_ToolingLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=NullTTS(),
        on_step=lambda i, tool, label, status: steps.append((i, tool, label, status)),
    )
    orch.run_once()
    assert steps == []  # voice turns surface via spoken cues, not the chat trace


def test_voice_cue_dedupes_repeated_phrases_within_a_turn() -> None:
    from autobot.orchestrator.wake_gate import PassThroughGate

    class _TwoToolLLM:
        def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
            execute(ToolCall(name="create_file", arguments={"path": "a"}))
            execute(ToolCall(name="create_file", arguments={"path": "b"}))
            return "done"

    tts = _RecordingTTS()
    orch = Orchestrator(
        settings=Settings(interaction_mode="voice", speak_acknowledgements=True),
        audio=_FakeAudio(),
        stt=_FakeSTT("make two files"),
        llm=_TwoToolLLM(),
        gate=_RecordingGate(),  # type: ignore[arg-type]
        wake_gate=PassThroughGate(),
        tts=tts,
    )
    orch.run_once()
    # Two identical-tool steps must not speak the same cue twice back-to-back.
    cues = tts.spoken[:-1]  # drop the final reply ("done")
    assert all(a != b for a, b in itertools.pairwise(cues))
