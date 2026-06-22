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
import re
import threading
import time
from collections.abc import Callable

import numpy as np

from autobot.config import Settings
from autobot.core.interfaces import AudioSource, LanguageModel, SpeechToText, TextToSpeech
from autobot.core.types import AudioClip, Risk, State, ToolCall, ToolResult, Transcription
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


# Words that, when an utterance ends on them with no terminal punctuation, strongly
# suggest the speaker was mid-thought when the silence endpoint fired ("…message to").
_CONTINUATION_WORDS = frozenset(
    {
        "and",
        "or",
        "but",
        "so",
        "to",
        "the",
        "a",
        "an",
        "of",
        "for",
        "with",
        "my",
        "your",
        "is",
        "are",
        "was",
        "were",
        "because",
        "that",
        "this",
        "then",
        "about",
        "on",
        "in",
        "at",
        "i",
        "we",
        "they",
        "he",
        "she",
        "it",
        "like",
        "if",
        "when",
        "while",
        "as",
        "into",
        "from",
        "over",
        "under",
        "than",
        "which",
        "who",
        "what",
        "where",
        "how",
        "let",
        "can",
        "could",
        "would",
        "should",
    }
)


# Whisper emits non-speech annotations on noise/silence — "(water splashing)",
# "[music]", "*sigh*", "♪" — and bare punctuation. These aren't real speech and
# must never reach the wake gate or the LLM (otherwise a fan or a splash becomes a
# "command", even running tools). Matches a whole transcript wrapped in (), [], or **.
_BRACKETED = re.compile(r"^[(\[*][^)\]*]*[)\]*]$")


def _is_nonspeech(text: str) -> bool:
    """True if a transcript is a Whisper non-speech artifact rather than real words.

    Catches empty/punctuation-only output and bracketed sound annotations like
    ``(water splashing)`` or ``[music]`` — so ambient noise can't trigger a turn.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if _BRACKETED.match(stripped):
        return True
    # No letters or digits at all (e.g. ".", "...", "♪♪") -> not speech.
    return re.search(r"[a-z0-9]", stripped.lower()) is None


def _looks_incomplete(text: str) -> bool:
    """Heuristic: does this transcript look cut off mid-thought?

    Conservative on purpose — true only when there's no sentence-final punctuation
    *and* the last word is a connective/function word, which together are a strong
    sign the silence endpoint fired while the user was still talking. Whisper
    punctuates finished sentences, so a complete thought won't match.
    """
    stripped = text.strip()
    if not stripped or stripped[-1] in ".!?":
        return False
    tokens = re.findall(r"[a-z0-9']+", stripped.lower())
    if len(tokens) < 2:
        return False
    return tokens[-1] in _CONTINUATION_WORDS


def _echo_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens, for comparing two utterances."""
    return re.findall(r"[a-z0-9']+", text.lower())


def _is_self_echo(heard: str, reply: str) -> bool:
    """True if ``heard`` looks like Jack's own ``reply`` bleeding into the mic.

    When echo cancellation doesn't fully remove Jack's voice, a barge-in can capture
    Jack's own words. We reject the capture if it's a contiguous fragment of the
    reply, or if almost all of its words appear in the reply — so the assistant can't
    act on what it just said. Conservative enough that a real interruption (new
    words) passes through.
    """
    h = _echo_tokens(heard)
    r = _echo_tokens(reply)
    if not h or not r:
        return False
    joined_h, joined_r = " ".join(h), " ".join(r)
    if joined_h in joined_r:  # verbatim fragment of the reply (e.g. its opener)
        return True
    rset = set(r)
    overlap = sum(1 for w in h if w in rset) / len(h)
    return overlap >= 0.75


def _ack_phrase(risk: Risk | None) -> str:
    """Pick an acknowledgement that fits what the tool is about to do.

    Args:
        risk: The tool's risk level, or ``None`` if the tool is unknown.

    Returns:
        A lookup (``READ_ONLY``) gets a neutral "checking" line; an action
        (``WRITE``/``DESTRUCTIVE``) gets a "confirming intent" line; an unknown
        tool falls back to a "thinking" line.
    """
    pool: tuple[str, ...]
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
        # Whether we're in an active conversation. Drives the orb's "listening"
        # animation: only animate while engaged, not during passive always-on
        # capture (otherwise ambient speech lights up the orb and it never rests).
        self._awake = False
        # An utterance the user spoke while barging in over a reply — processed as
        # the next turn instead of capturing fresh.
        self._pending_audio: AudioClip | None = None
        self._pending_started_at: float | None = None
        # The last reply text we spoke, so a barge-in that echoes it can be rejected.
        self._last_reply = ""

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

    def _reopen(
        self, audio: AudioClip, transcription: Transcription
    ) -> tuple[AudioClip, Transcription]:
        """Capture a short continuation for a cut-off phrase and re-transcribe (once).

        Returns the (possibly extended) audio and transcription. A no-op if the
        recorder can't continue or the user was actually finished.
        """
        cont = getattr(self._audio, "record_continuation", None)
        if not callable(cont):
            return audio, transcription
        _log.info("utterance looks cut off; re-opening text=%r", transcription.text)
        self._transcript.note(f"re-opening (sounded incomplete): {transcription.text!r}")
        extra = cont()
        if extra is None or extra.size == 0:
            return audio, transcription
        combined = np.concatenate([audio, extra]).astype(np.float32)
        new_transcription = self._stt.transcribe(combined)
        _log.info("re-opened text=%r", new_transcription.text)
        return combined, new_transcription

    def _set_awake(self, awake: bool) -> None:
        """Track conversation state and tell the recorder (drives the orb's listening)."""
        self._awake = awake
        set_awake = getattr(self._audio, "set_awake", None)
        if callable(set_awake):
            set_awake(awake)

    def _barge_in_capable(self) -> bool:
        """Barge-in only when enabled and the mic input is echo-cancelled (AEC).

        Without AEC the monitor would hear Jack's own voice and interrupt itself, so
        we require an AEC-active input before listening during playback.
        """
        return (
            self._settings.barge_in
            and callable(getattr(self._audio, "monitor_barge_in", None))
            and bool(getattr(self._audio, "aec_active", False))
        )

    def _respond(self, reply: str) -> AudioClip | None:
        """Speak ``reply``; if the user barges in, stop and return their utterance.

        Plays on a background thread while monitoring the mic for the user starting
        to speak. On barge-in we stop playback immediately and hand the captured
        utterance back so the loop treats it as the next turn. Returns ``None`` when
        the reply finished uninterrupted (or barge-in isn't available).
        """
        if not self._barge_in_capable():
            _log.debug(
                "barge-in off this reply: enabled=%s has_monitor=%s aec_active=%s",
                self._settings.barge_in,
                callable(getattr(self._audio, "monitor_barge_in", None)),
                bool(getattr(self._audio, "aec_active", False)),
            )
            self._tts.speak(reply)
            # Half-duplex: let the speaker tail decay before the mic re-opens, so Jack
            # never captures (and acts on) the end of its own voice.
            settle = max(0.0, self._settings.tts_settle_ms / 1000)
            if settle:
                time.sleep(settle)
            return None
        _log.info("speaking with barge-in monitoring (talk to interrupt)")
        done = threading.Event()

        def _play() -> None:
            try:
                self._tts.speak(reply)
            finally:
                done.set()

        thread = threading.Thread(target=_play, name="tts", daemon=True)
        thread.start()
        self._set_awake(True)  # animate "listening" if they cut in

        def _on_user_starts() -> None:
            # The instant the user speaks, cut the voice — don't wait for them to
            # finish the sentence (otherwise Jack talks over them).
            _log.info("barge-in: user started speaking — stopping playback now")
            self._tts.stop()

        # Presence + AEC checked in _barge_in_capable; not on the AudioSource protocol.
        barge = self._audio.monitor_barge_in(  # type: ignore[attr-defined]
            lambda: not done.is_set(), on_speech_start=_on_user_starts
        )
        self._tts.stop()  # safety: ensure stopped even if onset hook was missed
        thread.join()
        return barge if (barge is not None and barge.size) else None

    def run_once(self) -> None:
        """Run a single turn: listen, transcribe, gate the wake word, plan, respond."""
        pending = self._pending_audio  # local so mypy narrows it past the None check
        from_barge = pending is not None
        if pending is not None:
            # The user barged in over the last reply — process what they said now,
            # without capturing fresh. We're in a conversation, so stay awake.
            audio, started_at = pending, self._pending_started_at
            self._pending_audio = self._pending_started_at = None
            self._set_awake(True)
            self._sm.transition(State.LISTENING)
            self._sm.transition(State.TRANSCRIBING)
        else:
            # Only show the "listening" cue if we're engaged; passive capture is idle.
            self._set_awake(self._awake)
            self._sm.transition(State.LISTENING)
            audio = self._audio.record_clip()
            # When this utterance *began* — judge the follow-up window against speech
            # start, not end-of-capture, so a long phrase isn't dropped.
            started_at = getattr(self._audio, "last_speech_started_at", None)
            self._sm.transition(State.TRANSCRIBING)
        transcription = self._stt.transcribe(audio)
        if self._settings.save_audio and audio.size:
            from autobot.io.audio import save_wav

            clip = save_wav(self._settings.session_dir, audio, self._settings.sample_rate)
            _log.info("saved audio file=%s text=%r", clip, transcription.text)
            self._transcript.note(f"audio clip → {clip.name}  (heard: {transcription.text!r})")
        if transcription.is_empty or _is_nonspeech(transcription.text):
            # Nothing said, or just noise Whisper turned into "(water splashing)" /
            # bare punctuation. Drop it — and leave the conversation so background
            # noise can't keep firing turns; the wake word re-engages.
            _log.info("ignored reason=non_speech text=%r", transcription.text)
            if transcription.text.strip():
                self._transcript.note(f"ignored (not speech): {transcription.text!r}")
            self._set_awake(False)
            self._sm.transition(State.IDLE)
            return

        # A barge-in capture that echoes the reply Jack was just speaking is Jack's
        # own voice leaking past echo cancellation — not the user. Drop it (and stay
        # engaged) so the assistant can't act on its own words in a feedback loop.
        if from_barge and _is_self_echo(transcription.text, self._last_reply):
            _log.info("ignored reason=self_echo text=%r", transcription.text)
            self._transcript.note(f"ignored (echo of my own reply): {transcription.text!r}")
            self._set_awake(True)
            self._sm.transition(State.IDLE)
            return

        # If we seem to have cut the user off mid-thought, re-open briefly and append
        # rather than answer half a sentence (a safety net over the silence endpoint).
        if self._settings.reopen_on_incomplete and _looks_incomplete(transcription.text):
            audio, transcription = self._reopen(audio, transcription)

        result = self._wake_gate.process(transcription.text, started_at)
        if result.address is Address.IGNORED:
            # Heard speech, but it wasn't addressed to us — stay quiet and not awake
            # (so the orb rests and can auto-hide instead of animating on ambient talk).
            self._set_awake(False)
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
            self._set_awake(True)  # in a conversation now — animate the next turn
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
        # Remember what we're about to say so a barge-in echo of it can be rejected.
        self._last_reply = reply
        # Speak it — but if the user talks over Jack, stop and capture what they said.
        barge = self._respond(reply)
        if self._dismissed:
            # Dismissed: close the follow-up window so only the wake word returns,
            # and drop out of the conversation so the orb rests.
            self._wake_gate.end_follow_up()
            self._set_awake(False)
        else:
            self._wake_gate.mark_turn_complete()
            self._set_awake(True)  # stay engaged for the follow-up
        if barge is not None:
            # Process the interrupting utterance as the next turn.
            self._transcript.note("barge-in: user interrupted the reply")
            self._pending_audio = barge
            self._pending_started_at = getattr(self._audio, "last_speech_started_at", None)
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
