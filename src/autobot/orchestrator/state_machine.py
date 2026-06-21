"""The orchestrator state machine — the backbone every other phase plugs into.

:class:`StateMachine` is a tiny, pure transition validator (easy to unit-test).
:class:`Orchestrator` drives one turn through the real components, moving through
``idle → listening → transcribing → planning → (executing)* → responding`` with a
``clarifying`` branch when nothing is heard. Tool execution is delegated to an
injected executor (the permission gate), so the gate sits exactly between
*planning* and *executing*.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from autobot.config import Settings
from autobot.core.interfaces import AudioSource, LanguageModel, SpeechToText, TextToSpeech
from autobot.core.types import Risk, State, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.memory.store import MemoryStore
from autobot.orchestrator.wake_gate import Address, WakeGate
from autobot.session_log import NullTranscript, Transcript
from autobot.tools.permission import PermissionGate

_log = get_logger("orchestrator")

# The transitions the loop is allowed to make. Anything else is a bug.
_TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.ERROR}),
    State.LISTENING: frozenset({State.TRANSCRIBING, State.ERROR}),
    State.TRANSCRIBING: frozenset({State.PLANNING, State.CLARIFYING, State.IDLE, State.ERROR}),
    State.PLANNING: frozenset({State.EXECUTING, State.RESPONDING, State.CLARIFYING, State.ERROR}),
    State.EXECUTING: frozenset({State.EXECUTING, State.RESPONDING, State.ERROR}),
    State.RESPONDING: frozenset({State.IDLE, State.ERROR}),
    State.CLARIFYING: frozenset({State.IDLE, State.ERROR}),
    State.ERROR: frozenset({State.IDLE}),
}

StateListener = Callable[[State, State], None]
"""Called with ``(old_state, new_state)`` on every transition."""


class InvalidTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""


class StateMachine:
    """Tracks the current state and enforces the legal transition graph."""

    def __init__(
        self,
        initial: State = State.IDLE,
        on_change: StateListener | None = None,
    ) -> None:
        self._state = initial
        self._on_change = on_change

    @property
    def state(self) -> State:
        """The current state."""
        return self._state

    def can_transition(self, to: State) -> bool:
        """Whether moving from the current state to ``to`` is legal."""
        return to in _TRANSITIONS[self._state]

    def transition(self, to: State) -> None:
        """Move to ``to``, notifying the listener.

        Raises:
            InvalidTransition: If the move is not allowed from the current state.
        """
        if not self.can_transition(to):
            raise InvalidTransitionError(f"{self._state.value} -> {to.value}")
        old, self._state = self._state, to
        _log.debug("state %s -> %s", old.value, to.value)
        if self._on_change is not None:
            self._on_change(old, to)


def _print_transition(_old: State, new: State) -> None:
    """Default listener: show the state the assistant is entering."""
    print(f"[state] {new.value}")


# Short spoken acknowledgements so a slow tool call doesn't leave dead air. The
# pool is chosen to fit what the tool is about to do (see _ack_phrase):
#   - looking something up (READ_ONLY): a neutral "checking" line
#   - taking an action  (WRITE/DESTRUCTIVE): a "confirming intent" line
#   - anything we can't classify: a "thinking" line
_NEUTRAL_ACKS = (
    "Let me look into that.",
    "Checking now.",
    "Let me pull that up.",
    "Give me a sec.",
    "Hang on a moment.",
    "Looking that up.",
    "Let me find out.",
)
_THINKING_ACKS = (
    "Let me think.",
    "Hmm, one sec.",
    "Working on it.",
    "Just a moment.",
)
_CONFIRMING_ACKS = (
    "Got it, checking.",
    "Sure thing.",
    "Right away.",
    "Alright, let me see.",
)


def _ack_phrase(risk: Risk | None) -> str:
    """Pick an acknowledgement that fits what the tool is about to do.

    Args:
        risk: The tool's risk level, or ``None`` if the tool is unknown.

    Returns:
        A lookup (``READ_ONLY``) gets a neutral "checking" line; an action
        (``WRITE``/``DESTRUCTIVE``) gets a "confirming intent" line; an unknown
        tool falls back to a "thinking" line.
    """
    if risk is None:
        pool = _THINKING_ACKS
    elif risk >= Risk.WRITE:
        pool = _CONFIRMING_ACKS
    else:
        pool = _NEUTRAL_ACKS
    return random.choice(pool)


class Orchestrator:
    """Drives one interaction turn through the components and the permission gate."""

    def __init__(
        self,
        settings: Settings,
        audio: AudioSource,
        stt: SpeechToText,
        llm: LanguageModel,
        gate: PermissionGate,
        wake_gate: WakeGate,
        tts: TextToSpeech,
        transcript: Transcript | None = None,
        on_state: StateListener | None = _print_transition,
        memory: MemoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._audio = audio
        self._stt = stt
        self._llm = llm
        self._gate = gate
        self._wake_gate = wake_gate
        self._tts = tts
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        self._sm = StateMachine(on_change=on_state)
        self._acknowledged = False  # spoke a filler this turn?
        self._dismissed = False  # did this turn call the dismiss tool ("go away")?

    def _greeting(self) -> str:
        """The reply to a bare wake word — name-aware, and a first hello if new."""
        if self._memory is not None:
            name = self._memory.get_name()
            if name:
                return f"Yes, {name}?"
            return (
                "Hey, I'm Jack — your friendly assistant for getting things done on "
                "your Mac. I don't think we've met — what's your name?"
            )
        return "Yes?"

    @property
    def state(self) -> State:
        """The orchestrator's current state."""
        return self._sm.state

    def mark_llm_dirty(self) -> None:
        """Ask the LLM to rebuild from fresh settings before the next turn.

        Called by the daemon when the Settings view changes the provider/model/key,
        so the change takes effect without a restart.
        """
        reload_fn = getattr(self._llm, "mark_dirty", None)
        if callable(reload_fn):
            reload_fn()

    def mark_stt_dirty(self) -> None:
        """Ask the STT engine to reload its model before the next transcription."""
        reload_fn = getattr(self._stt, "mark_dirty", None)
        if callable(reload_fn):
            reload_fn()

    def mark_settings_changed(self) -> None:
        """Reload everything a settings change can affect (LLM + STT), no restart."""
        self.mark_llm_dirty()
        self.mark_stt_dirty()

    def _execute(self, call: ToolCall) -> ToolResult:
        """Executor handed to the LLM: mark EXECUTING and run through the gate."""
        self._sm.transition(State.EXECUTING)
        if call.name == "dismiss":
            # The user asked Jack to go away: don't keep a follow-up window open —
            # require the wake word to come back (handled after the turn).
            self._dismissed = True
        # Acknowledge once per turn so a slow tool call isn't silent — phrased to
        # match the tool's nature (lookup vs action), from its risk level.
        if self._settings.speak_acknowledgements and not self._acknowledged:
            self._acknowledged = True
            risk_of = getattr(self._gate, "risk_of", None)
            risk = risk_of(call.name) if callable(risk_of) else None
            self._tts.speak(_ack_phrase(risk))
        result = self._gate.execute(call)
        self._transcript.tool(call.name, call.arguments, result.ok, result.content)
        return result

    def run_once(self) -> None:
        """Run a single turn: listen, transcribe, gate the wake word, plan, respond."""
        self._sm.transition(State.LISTENING)
        audio = self._audio.record_clip()

        self._sm.transition(State.TRANSCRIBING)
        transcription = self._stt.transcribe(audio)
        if self._settings.save_audio and audio.size:
            from autobot.io.audio import save_wav

            clip = save_wav(self._settings.session_dir, audio, self._settings.sample_rate)
            _log.info("saved audio file=%s text=%r", clip, transcription.text)
            self._transcript.note(f"audio clip → {clip.name}  (heard: {transcription.text!r})")
        if transcription.is_empty:
            _log.debug("ignored reason=no_speech")
            self._sm.transition(State.IDLE)
            return

        result = self._wake_gate.process(transcription.text)
        if result.address is Address.IGNORED:
            # Heard speech, but it wasn't addressed to us — stay quiet.
            _log.info("ignored reason=not_addressed text=%r %s", transcription.text, result.detail)
            self._transcript.note(
                f"ignored (not addressed): {transcription.text!r}  [{result.detail}]"
            )
            self._sm.transition(State.IDLE)
            return

        if result.address is Address.GREETED:
            # Wake word with no command — acknowledge and open the follow-up window.
            _log.info("greeted text=%r", transcription.text)
            self._transcript.user(transcription.text, transcription.confidence)
            self._sm.transition(State.PLANNING)
            self._sm.transition(State.RESPONDING)
            greeting = self._greeting()
            print(f"[autobot] {greeting}\n")
            self._transcript.assistant(greeting)
            self._tts.speak(greeting)
            self._wake_gate.mark_turn_complete()
            self._sm.transition(State.IDLE)
            return

        command = result.command
        _log.info("heard text=%r confidence=%.2f", command, transcription.confidence)
        print(f"[you] {command}   (confidence {transcription.confidence:.2f})")
        self._transcript.user(command, transcription.confidence)

        self._sm.transition(State.PLANNING)
        self._acknowledged = False  # reset per turn; _execute may speak a filler
        self._dismissed = False  # reset per turn; set if the dismiss tool runs
        started = time.perf_counter()
        reply = self._llm.run_turn(command, self._execute)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self._sm.transition(State.RESPONDING)
        _log.info("replied chars=%d latency_ms=%d", len(reply), elapsed_ms)
        print(f"[autobot] {reply}\n")
        self._transcript.assistant(reply)
        self._tts.speak(reply)
        if self._dismissed:
            # Dismissed: close the follow-up window so only the wake word returns.
            self._wake_gate.end_follow_up()
        else:
            self._wake_gate.mark_turn_complete()
        self._sm.transition(State.IDLE)

    def run(self) -> None:
        """Run the interaction loop until interrupted with Ctrl-C."""
        if self._settings.input_mode == "ptt":
            trigger = "push-to-talk (press Enter)"
        elif self._settings.wake_detector == "openwakeword":
            trigger = f'hands-free (say "{self._settings.wake_model.replace("_", " ")}")'
        else:
            trigger = f'hands-free (say "{self._settings.wake_phrase}, …")'
        print("=" * 60)
        print(" Autobot — orchestrator + guarded tools")
        print(f" STT: {self._settings.stt_model}   LLM: {self._settings.llm_model}")
        print(f" Input: {trigger}")
        print(f" Workspace: {self._settings.sandbox_dir}")
        print(' Try: "create a file notes.txt", "delete notes.txt"')
        print(" Ctrl-C to quit")
        print("=" * 60)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\nBye.")
                self._transcript.close()
                return
            except Exception as exc:  # keep the loop alive on unexpected failures
                _log.exception("turn failed error=%s", exc)
                self._transcript.note(f"ERROR: {exc}")
                print(f"[error] {exc}  (see log for details)")
                # Let the user know without dumping a traceback at them.
                try:
                    self._tts.speak("Sorry, something went wrong.")
                except Exception:  # never let error-handling raise
                    _log.exception("tts failed while reporting an error")
                # Recover back to a clean idle state for the next turn.
                if self._sm.can_transition(State.ERROR):
                    self._sm.transition(State.ERROR)
                self._sm.transition(State.IDLE)
