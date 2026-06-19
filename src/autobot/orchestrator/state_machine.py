"""The orchestrator state machine — the backbone every other phase plugs into.

:class:`StateMachine` is a tiny, pure transition validator (easy to unit-test).
:class:`Orchestrator` drives one turn through the real components, moving through
``idle → listening → transcribing → planning → (executing)* → responding`` with a
``clarifying`` branch when nothing is heard. Tool execution is delegated to an
injected executor (the permission gate), so the gate sits exactly between
*planning* and *executing*.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from autobot.config import Settings
from autobot.core.interfaces import AudioSource, LanguageModel, SpeechToText
from autobot.core.types import State, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.orchestrator.wake_gate import Address, WakeGate
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
        on_state: StateListener | None = _print_transition,
    ) -> None:
        self._settings = settings
        self._audio = audio
        self._stt = stt
        self._llm = llm
        self._gate = gate
        self._wake_gate = wake_gate
        self._sm = StateMachine(on_change=on_state)

    @property
    def state(self) -> State:
        """The orchestrator's current state."""
        return self._sm.state

    def _execute(self, call: ToolCall) -> ToolResult:
        """Executor handed to the LLM: mark EXECUTING and run through the gate."""
        self._sm.transition(State.EXECUTING)
        return self._gate.execute(call)

    def run_once(self) -> None:
        """Run a single turn: listen, transcribe, gate the wake word, plan, respond."""
        self._sm.transition(State.LISTENING)
        audio = self._audio.record_clip()

        self._sm.transition(State.TRANSCRIBING)
        transcription = self._stt.transcribe(audio)
        if transcription.is_empty:
            _log.debug("ignored reason=no_speech")
            self._sm.transition(State.IDLE)
            return

        result = self._wake_gate.process(transcription.text)
        if result.address is Address.IGNORED:
            # Heard speech, but it wasn't addressed to us — stay quiet.
            _log.info("ignored reason=not_addressed text=%r", transcription.text)
            self._sm.transition(State.IDLE)
            return

        if result.address is Address.GREETED:
            # Wake word with no command — acknowledge and open the follow-up window.
            _log.info("greeted text=%r", transcription.text)
            self._sm.transition(State.PLANNING)
            self._sm.transition(State.RESPONDING)
            print("[autobot] Yes?\n")
            self._wake_gate.mark_turn_complete()
            self._sm.transition(State.IDLE)
            return

        command = result.command
        _log.info("heard text=%r confidence=%.2f", command, transcription.confidence)
        print(f"[you] {command}   (confidence {transcription.confidence:.2f})")

        self._sm.transition(State.PLANNING)
        started = time.perf_counter()
        reply = self._llm.run_turn(command, self._execute)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self._sm.transition(State.RESPONDING)
        _log.info("replied chars=%d latency_ms=%d", len(reply), elapsed_ms)
        print(f"[autobot] {reply}\n")
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
                return
            except Exception as exc:  # keep the loop alive on unexpected failures
                _log.exception("turn failed error=%s", exc)
                print(f"[error] {exc}  (see log for details)")
                # Recover back to a clean idle state for the next turn.
                if self._sm.can_transition(State.ERROR):
                    self._sm.transition(State.ERROR)
                self._sm.transition(State.IDLE)
