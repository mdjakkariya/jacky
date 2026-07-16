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
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autobot.agent.coder_turn import CoderTurnDriver
    from autobot.mcp.provider import McpProvider
    from autobot.tasks import Task, TaskRegistry

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

# How long to stay in chat before actually releasing the mic. Debounced so a quick
# chat⇄voice toggle keeps the audio engine alive (instant, no churn) instead of
# tearing it down and rebuilding it (which can briefly drop macOS AEC).
_VOICE_IO_RELEASE_DELAY_S = 5.0

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

    def reset(self, to: State = State.IDLE) -> None:
        """Force the state to ``to``, bypassing the transition graph.

        For entering a turn from an unknown state (e.g. a typed chat turn while the
        voice loop left the machine in LISTENING) where the strict graph doesn't
        apply. Notifies the listener only when the state actually changes.
        """
        old, self._state = self._state, to
        if old is not to and self._on_change is not None:
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

# Warm sign-offs spoken/shown when the user dismisses Jack ("go away", "bye"). Used as
# a fallback so a dismiss always ends on a friendly note, even if the model returns no
# trailing text. Kept emoji-free so they read cleanly when spoken (TTS).
_GOODBYES = (
    "See you later!",
    "Talk soon!",
    "Catch you later!",
    "Bye for now!",
    "Take care!",
    "I'll be here when you need me.",
    "Alright, see you!",
    "Happy to help — see you soon!",
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


def _goodbye() -> str:
    """A random warm sign-off for when the user dismisses Jack."""
    return random.choice(_GOODBYES)


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


def _format_ack(template: str, arguments: dict[str, object]) -> str:
    """Fill a tool's ack ``{target}`` with the call's main argument, or drop it.

    "Opening {target}." + ``{"name": "Spotify"}`` → "Opening Spotify."; with no
    usable argument it reads "Opening that." An empty template stays empty (silent).
    """
    if "{target}" not in template:
        return template
    target = next((v.strip() for v in arguments.values() if isinstance(v, str) and v.strip()), None)
    if target:
        return template.replace("{target}", target)
    return re.sub(r"\s{2,}", " ", template.replace("{target}", "that")).strip()


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
        on_context: Callable[[dict[str, Any]], None] | None = None,
        on_step: Callable[[int, str, str, str], None] | None = None,
        on_show: Callable[[], None] | None = None,
        release_voice_io: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._audio = audio
        # Releases the lazily-built voice I/O (mic + TTS). Called (debounced) when
        # switching to chat so the mic is freed and macOS stops ducking other audio;
        # the next voice turn rebuilds it. None in tests / headless runs without a UI.
        self._release_voice_io = release_voice_io
        # Debounce the release: a quick chat⇄voice toggle shouldn't tear down and
        # rebuild the audio engine (that churns macOS Voice-Processing and can drop
        # AEC). We arm a timer on entering chat and cancel it if voice resumes first.
        self._voice_io_release_timer: threading.Timer | None = None
        self._voice_io_timer_lock = threading.Lock()
        self._stt = stt
        self._llm = llm
        self._gate = gate
        self._wake_gate = wake_gate
        self._tts = tts
        self._transcript = transcript or NullTranscript()
        self._memory = memory
        # Sink for per-turn context-window usage (drives the chat meter), if wired.
        self._on_context = on_context
        # Sink for per-tool-step progress (running/done/failed), if wired.
        self._on_step = on_step
        # Asks the UI to re-show the orb (voice UI). Fired when a voice turn is
        # addressed to Jack while the orb may be hidden, so "Jack, …" brings it back.
        self._on_show = on_show
        self._sm = StateMachine(on_change=on_state)
        self._step_index = 0  # tool-step counter within the current turn
        self._last_spoken_ack = ""  # last per-step voice cue, to dedupe back-to-back repeats
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
        # "voice" or "chat". In chat mode the run loop idles (no mic) and turns come
        # in as typed text via run_text_turn; replies are returned, not spoken.
        self._mode = settings.interaction_mode
        self._text_mode = False  # True while handling a typed turn (suppresses TTS)
        # Snapshot the settings that actually require a model reload, so a frequent,
        # cheap change (toggling voice⇄chat when the drawer opens/closes) doesn't
        # needlessly reconnect the cloud LLM or reload the STT model every time.
        self._llm_keys = (settings.llm_provider, settings.llm_model, settings.anthropic_model)
        self._stt_keys = (settings.stt_engine, settings.stt_model)
        # One turn at a time. The voice loop (this thread) and chat turns (the daemon
        # worker thread) share one state machine; without this they interleave their
        # transitions and crash ("listening -> executing", "planning -> transcribing").
        # Re-entrant so a turn can call helpers that also take it.
        self._turn_lock = threading.RLock()
        # Set by the composition root (app.build). The daemon delegates /mcp/*
        # requests to the live manager via this provider, which can also create or
        # tear down the manager at runtime when ``allow_mcp`` is toggled (no restart).
        self.mcp_provider: McpProvider | None = None
        # Set by the composition root when allow_meetings is True. The daemon
        # delegates /meeting/* HTTP actions to the recorder via this attribute.
        self.meeting_recorder: Any = None
        # Set by app.build() in the coder profile; None for the assistant. Backs the
        # daemon's /coder/turn + /coder/reply endpoints (plan → approve → act).
        self.coder_driver: CoderTurnDriver | None = None
        # Set by app.build() in the coder profile; None for the assistant. The process-global
        # async-task registry, whose settle events back the daemon's /coder/events stream.
        self.coder_task_registry: TaskRegistry | None = None

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

    def in_chat_mode(self) -> bool:
        """True when typed-chat is active, so the voice orb shouldn't be driven.

        Covers both a steady chat mode and the moment a typed turn is being handled
        (so a chat turn's THINKING/TALKING transitions don't wake the orb).
        """
        return self._mode == "chat" or self._text_mode

    def new_chat_session(self) -> None:
        """Discard the conversation and start fresh (the chat's "New chat" action).

        Wipes the LLM's history/summary so the next turn starts clean, clears the
        last reply, and rests the state machine at IDLE. Held under the turn lock so
        it can never land in the middle of an in-flight turn (voice or typed).
        """
        with self._turn_lock:
            reset = getattr(self._llm, "new_session", None)
            if callable(reset):
                reset()
            self._last_reply = ""
            self._sm.reset(State.IDLE)
            # Forget "allow this session" grants so a fresh chat re-confirms actions.
            self._gate.clear_session_grants()
            # Scope the concise debug report to this fresh session: breadcrumbs from
            # before "New chat" stay in the full report but drop out of the dev view.
            from autobot.diagnostics import get_buffer

            get_buffer().mark_session()
        _log.info("new chat session started")

    def list_sessions(self) -> list[dict[str, Any]]:
        """Summaries of stored agent sessions (id/cwd/model/mtime), for the daemon."""
        fn = getattr(self._llm, "list_sessions", None)
        result = fn() if callable(fn) else []
        return result if isinstance(result, list) else []

    def resume_session(self, session_id: str) -> bool:
        """Resume a stored agent session by id. Held under the turn lock like a new turn."""
        with self._turn_lock:
            fn = getattr(self._llm, "resume", None)
            return bool(fn(session_id)) if callable(fn) else False

    def run_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one registered tool through the permission gate — for a UI action.

        Backs a clicked action card (e.g. "Open" on a file result): the named tool
        runs through the *same* gate (risk classification + audit) the model uses, so
        nothing bypasses it — but with no LLM call, so a click costs no tokens and is
        instant. Held under the turn lock so it can't interleave with a voice/typed
        turn. Returns the tool's result text (a short failure message if it failed).
        """
        call = ToolCall(name=name, arguments=dict(arguments or {}))
        with self._turn_lock:
            _log.info("ui action tool=%s args=%s", name, call.arguments)
            result = self._gate.execute(call)
            self._transcript.tool(call.name, call.arguments, result.ok, result.content)
        return result.content

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
        """Apply a settings change with no restart — reloading *only* what changed.

        Reloads the LLM or STT model only when their settings actually differ from
        what's loaded, so the frequent voice⇄chat toggle (every drawer open/close)
        doesn't reconnect the cloud LLM or reload the STT model for nothing.
        """
        new = Settings.load()
        llm_keys = (new.llm_provider, new.llm_model, new.anthropic_model)
        if llm_keys != self._llm_keys:
            self._llm_keys = llm_keys
            self.mark_llm_dirty()
        stt_keys = (new.stt_engine, new.stt_model)
        if stt_keys != self._stt_keys:
            self._stt_keys = stt_keys
            self.mark_stt_dirty()
        # Pick up a voice⇄chat switch (applies next loop tick) — always cheap.
        old_mode, self._mode = self._mode, new.interaction_mode
        if self._mode != old_mode:
            if self._mode == "chat":
                # Leaving voice: arm a debounced release. If voice resumes within the
                # window we cancel it and keep the mic alive — no teardown/rebuild churn.
                self._schedule_voice_io_release()
            else:
                # Back to voice: keep the mic; cancel any pending release.
                self._cancel_voice_io_release()

    def _schedule_voice_io_release(self) -> None:
        """Arm a debounced mic release; resets any pending timer. Safe with no UI."""
        if self._release_voice_io is None:
            return
        with self._voice_io_timer_lock:
            if self._voice_io_release_timer is not None:
                self._voice_io_release_timer.cancel()
            timer = threading.Timer(_VOICE_IO_RELEASE_DELAY_S, self._release_if_still_chat)
            timer.daemon = True
            self._voice_io_release_timer = timer
            timer.start()

    def _cancel_voice_io_release(self) -> None:
        """Cancel a pending mic release (voice resumed before the debounce elapsed)."""
        with self._voice_io_timer_lock:
            if self._voice_io_release_timer is not None:
                self._voice_io_release_timer.cancel()
                self._voice_io_release_timer = None

    def _release_if_still_chat(self) -> None:
        """Debounce timer fired: release the voice I/O, but only if still in chat."""
        with self._voice_io_timer_lock:
            self._voice_io_release_timer = None
        if self._mode == "chat":
            self._release_voice_io_now()

    def _release_voice_io_now(self) -> None:
        """Tear down the lazily-built voice I/O, if any (mic + TTS). Never raises."""
        if self._release_voice_io is None:
            return
        try:
            self._release_voice_io()
        except Exception:  # teardown must never break a settings change
            _log.exception("releasing voice I/O failed")

    def _execute(self, call: ToolCall) -> ToolResult:
        """Executor handed to the LLM: mark EXECUTING, surface the step, run the gate."""
        self._sm.transition(State.EXECUTING)
        if call.name == "dismiss":
            self._dismissed = True
        index = self._step_index
        self._step_index += 1
        label = self._step_label(call)
        self._emit_step(index, call.name, label, "running")
        # Voice: a short cue per step so a multi-step chain isn't silent — deduped so
        # the same phrase isn't spoken back-to-back, and silent (ack="") tools say nothing.
        if self._settings.speak_acknowledgements and not self._text_mode:
            phrase = self._ack_for(call)
            if phrase and phrase != self._last_spoken_ack:
                self._last_spoken_ack = phrase
                self._tts.speak(phrase)
        result = self._gate.execute(call)
        self._emit_step(index, call.name, label, "done" if result.ok else "failed")
        self._transcript.tool(call.name, call.arguments, result.ok, result.content)
        return result

    def _emit_step(self, index: int, tool: str, label: str, status: str) -> None:
        """Publish a tool-step update to the UI, if a sink is wired. Never raises.

        The step trace is a *chat-drawer* surface; a voice turn surfaces its steps via
        spoken cues instead. So we don't emit for a voice turn — otherwise a voice
        turn's rows render in the chat drawer with nothing to clear them (clearSteps
        only runs on a chat reply), and pile up as a stale trace across turns.
        """
        if self._on_step is None or not self.in_chat_mode():
            return
        try:
            self._on_step(index, tool, label, status)
        except Exception:  # a UI hiccup must never break a turn
            _log.exception("on_step sink failed")

    def _step_label(self, call: ToolCall) -> str:
        """A short human label for a tool step: its ack phrasing, else a tidy name."""
        ack_of = getattr(self._gate, "ack_of", None)
        ack = ack_of(call.name) if callable(ack_of) else None
        if ack:  # the tool's own phrasing, e.g. "Opening {target}" -> "Opening Spotify"
            return _format_ack(ack, call.arguments).rstrip(".")
        return call.name.replace("_", " ").capitalize()

    def _set_delivery(self, mode: str) -> None:
        """Tell the LLM whether this turn's reply is spoken (voice) or shown (chat).

        So the model styles the reply for its medium — short and unformatted when
        spoken, concise text (light markdown allowed) when typed.
        """
        setter = getattr(self._llm, "set_delivery_mode", None)
        if callable(setter):
            setter(mode)

    def _emit_context(self) -> None:
        """Publish this turn's context-window usage to the chat meter, if wired."""
        if self._on_context is None:
            return
        usage = getattr(self._llm, "context_usage", None)
        if callable(usage):
            info = usage()
            if info:
                self._on_context(info)

    def _llm_unavailable_message(self, exc: Exception) -> str | None:
        """A clear, actionable line for a known LLM-availability failure, else ``None``.

        The local backend (Ollama) and the cloud backend fail differently; a generic
        "something went wrong" leaves the user stuck. We only special-case the
        connection failure (the common "the model isn't running / unreachable" case)
        and let anything genuinely unexpected fall through to the generic handler.
        """
        if not isinstance(exc, ConnectionError):
            return None
        if Settings.load().llm_provider == "anthropic":
            return "I can't reach Claude right now — please check your internet connection."
        return (
            "I can't reach the local model. Make sure Ollama is running — open the "
            "Ollama app (or run `ollama serve`) — then try again."
        )

    def _ack_for(self, call: ToolCall) -> str:
        """The spoken filler for a call: the tool's own ack, else a risk-based one.

        A tool's ``ack`` of ``""`` means stay silent; ``None`` (or an unknown tool)
        falls back to a generic phrase chosen by risk level.
        """
        ack_of = getattr(self._gate, "ack_of", None)
        ack = ack_of(call.name) if callable(ack_of) else None
        if ack is not None:
            return _format_ack(ack, call.arguments)
        risk_of = getattr(self._gate, "risk_of", None)
        risk = risk_of(call.name) if callable(risk_of) else None
        return _ack_phrase(risk)

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

    def _show_orb(self) -> None:
        """Ask the UI to bring the voice orb back (it may have been hidden).

        A voice turn only runs while we're in voice mode, so being addressed by
        voice should always surface the orb — even if the user tucked it away with
        the global shortcut or the tray, where the engine never learned it hid.
        """
        if self._on_show is not None:
            self._on_show()

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
        else:
            # Re-check the mode right before we (possibly) build + open the mic, to
            # avoid a switch-to-chat that landed just after the run-loop's top check
            # from opening the mic again. Closing the source (release) also unblocks
            # an already-running capture, so the two together cover the window.
            if self._mode == "chat":
                self._sm.reset(State.IDLE)
                return
            # Only show the "listening" cue if we're engaged; passive capture is idle.
            self._set_awake(self._awake)
            self._sm.reset(State.LISTENING)
            # Capture is lock-free on purpose: in wake mode record_clip blocks until
            # the wake word, possibly forever, so holding the turn lock here would
            # let a chat turn wait indefinitely. We take the lock only once we have
            # audio to process.
            audio = self._audio.record_clip()
            # When this utterance *began* — judge the follow-up window against speech
            # start, not end-of-capture, so a long phrase isn't dropped.
            started_at = getattr(self._audio, "last_speech_started_at", None)
        # Process this turn under the turn lock so its state-machine transitions can't
        # interleave with a chat turn's (which would crash, e.g. "planning ->
        # transcribing"). reset() forces us into TRANSCRIBING regardless of any state a
        # chat turn left behind while we were capturing.
        with self._turn_lock:
            if self._mode == "chat":
                # Switched to chat while capturing — abandon this voice turn cleanly.
                self._sm.reset(State.IDLE)
                return
            self._sm.reset(State.TRANSCRIBING)
            self._process_voice_turn(audio, started_at, from_barge)

    def _process_voice_turn(
        self, audio: AudioClip, started_at: float | None, from_barge: bool
    ) -> None:
        """Transcribe, gate the wake word, plan, and respond — under the turn lock."""
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

        # Addressed to Jack (a greeting or a command): we're engaged by voice, so make
        # sure the orb is visible — it may have been hidden via the shortcut/tray.
        self._show_orb()

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
        self._step_index = 0
        self._last_spoken_ack = ""
        self._dismissed = False  # reset per turn; set if the dismiss tool runs
        self._set_delivery("voice")  # this reply is spoken
        started = time.perf_counter()
        reply = self._llm.run_turn(command, self._execute)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # A dismiss should always end on a warm goodbye — if the model added no
        # trailing text after calling the tool, supply a random sign-off to speak.
        if self._dismissed and not reply.strip():
            reply = _goodbye()

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

    def run_text_turn(self, text: str) -> str:
        """Handle one typed turn (chat mode): same LLM + gate, reply returned as text.

        No mic, STT, or wake gate — typing is addressing Jack — and the reply is
        returned for the chat UI rather than spoken. Tool calls and confirmations
        still flow through the permission gate.
        """
        text = text.strip()
        if not text:
            return ""
        # Typing *is* chat. Force chat mode now (don't rely on the UI's mode POST,
        # which can be missed — then the voice loop keeps listening to the room and
        # speaking while you type) and cut off any reply Jack is mid-sentence on, so
        # the mic loop goes quiet immediately. The voice loop sees _mode next tick
        # and idles; closing the drawer restores voice via mark_settings_changed.
        self._mode = "chat"
        # Typing forces chat even if the UI's mode POST was missed; arm the debounced
        # mic release too (resets while you keep typing; frees it ~quiet-spell later).
        self._schedule_voice_io_release()
        self._tts.stop()
        # Serialise against the voice loop: wait for any in-flight voice turn to
        # finish before driving the shared state machine (and vice versa).
        with self._turn_lock:
            return self._run_text_turn_locked(text)

    def _run_text_turn_locked(self, text: str) -> str:
        """Body of :meth:`run_text_turn`, run while holding the turn lock."""
        self._text_mode = True
        try:
            self._transcript.user(text, 1.0)
            # Force the state (the voice loop, paused in chat mode, may have left the
            # machine in LISTENING) — go straight to "thinking" for a typed turn.
            self._sm.reset(State.PLANNING)
            self._step_index = 0
            self._last_spoken_ack = ""
            self._dismissed = False
            self._set_delivery("chat")  # this reply is shown as text, not spoken
            started = time.perf_counter()
            try:
                reply = self._llm.run_turn(text, self._execute)
            except Exception as exc:
                # A clear, actionable message (e.g. Ollama not running) beats a failed
                # request / empty bubble in the chat drawer. Unexpected errors re-raise.
                msg = self._llm_unavailable_message(exc)
                if msg is None:
                    raise
                _log.warning("chat turn: LLM unavailable: %s", exc)
                reply = msg
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            # The model sometimes runs a tool and returns no trailing text (notably
            # dismiss). Never show the chat an empty bubble — fall back to a short,
            # fitting line so a typed turn always gets a reply.
            if not reply.strip():
                reply = _goodbye() if self._dismissed else "Done. ✅"
            self._sm.reset(State.RESPONDING)
            _log.info("chat replied chars=%d latency_ms=%d", len(reply), elapsed_ms)
            self._transcript.assistant(reply)
            self._last_reply = reply
            self._emit_context()  # update the chat context meter
            self._sm.reset(State.IDLE)
            return reply
        finally:
            self._text_mode = False

    def start_coder_stream(self, text: str) -> Iterator[dict[str, Any]]:
        """Begin a coder turn and stream its events (coder profile only)."""
        if self.coder_driver is None:
            yield {"status": "error", "reply": "coding turns aren't available here."}
            return
        self._record_transcript(lambda: self._transcript.user(text, 1.0))
        yield from self._record_coder_events(self.coder_driver.start_stream(text))

    def interrupt_coder(self) -> bool:
        """Request the running coder turn stop (esc-to-interrupt); True if one was active."""
        return self.coder_driver.interrupt() if self.coder_driver is not None else False

    def reply_coder_stream(self, value: str, text: str = "") -> Iterator[dict[str, Any]]:
        """Deliver the CLI's answer and stream the next phase's events (coder profile only)."""
        if self.coder_driver is None:
            yield {"status": "error", "reply": "coding turns aren't available here."}
            return
        # The gate answer is part of the conversation: a refine/edit carries new instruction
        # text (record it as a user turn); a bare approve/reject/yes/no is just a note.
        refinement = text.strip()
        if refinement:
            self._record_transcript(lambda: self._transcript.user(refinement, 1.0))
        else:
            self._record_transcript(lambda: self._transcript.note(f"gate answered: {value}"))
        yield from self._record_coder_events(self.coder_driver.reply_stream(value, text))

    def _record_coder_events(self, events: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Mirror a coder event stream into the readable transcript as it passes through.

        The coder previously left the Markdown transcript with only cloud-usage lines — the
        actual conversation (the plan, tool activity, the final reply) never reached it. This
        re-yields every event untouched while recording the human-readable ones, so the file
        shows what happened without changing what the CLI receives.
        """
        for evt in events:
            self._record_one_coder_event(evt)
            yield evt

    def _record_one_coder_event(self, evt: dict[str, Any]) -> None:
        """Write one coder stream event to the readable transcript (best-effort)."""

        def write() -> None:
            if evt.get("type") == "tool" and evt.get("event") == "end":
                self._transcript.tool(
                    str(evt.get("name", "")),
                    {},
                    bool(evt.get("ok", True)),
                    str(evt.get("label", "")),
                )
                return
            status = evt.get("status")
            if status == "pending":
                self._transcript.note(f"awaiting confirmation: {evt.get('prompt', '')}")
            elif status in ("plan", "done", "error"):
                self._transcript.assistant(str(evt.get("reply", "")))

        self._record_transcript(write)

    @staticmethod
    def _record_transcript(write: Callable[[], None]) -> None:
        """Run a transcript write; a transcript failure must never break the stream."""
        try:
            write()
        except Exception:  # a readable-log write is best-effort — never fail a turn for it
            _log.debug("coder transcript write failed; continuing", exc_info=True)

    def undo_coder(self) -> tuple[bool, str]:
        """Restore the most recent workspace checkpoint (coder profile only)."""
        if self.coder_driver is None:
            return False, "Undo isn't available here."
        return self.coder_driver.undo()

    def list_coder_checkpoints(self) -> list[dict[str, str]]:
        """List workspace checkpoints newest-first (coder profile only)."""
        return self.coder_driver.list_checkpoints() if self.coder_driver is not None else []

    def coder_usage(self) -> dict[str, Any]:
        """The usage payload for GET /coder/usage: live context + ledger rollups."""
        from datetime import datetime

        from autobot.config import Settings
        from autobot.usage import ledger, rollup

        ctx = None
        sid = None
        ctx_fn = getattr(self._llm, "context_usage", None)
        if callable(ctx_fn):
            ctx = ctx_fn()
        sid_fn = getattr(self._llm, "session_id", None)
        if callable(sid_fn):
            sid = sid_fn()
        rolls = rollup.summarize(ledger.read(), now=datetime.now(), session_id=sid)
        return {
            "ctx": ctx,
            "provider": Settings.load().llm_provider,
            "model": (ctx or {}).get("model"),
            "rollups": rolls.to_dict(),
        }

    def resume_coder_session(self, session_id: str) -> bool:
        """Resume a stored coder session through the driver's lock (coder profile only)."""
        return self.coder_driver.resume(session_id) if self.coder_driver is not None else False

    def new_coder_session(self) -> bool:
        """Start a fresh coder session through the driver's lock (coder profile only)."""
        return self.coder_driver.new_session() if self.coder_driver is not None else False

    def subscribe_coder_events(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Push a compact event to ``callback`` whenever a background task settles.

        Backs the daemon's persistent ``/coder/events`` stream: each finished
        :class:`~autobot.tasks.Task` becomes a ``{"type": "task", ...}`` dict so an idle CLI
        can pick the result up (auto-resume). Returns an unsubscribe; a no-op returning a
        no-op when there's no task registry (assistant profile).
        """
        if self.coder_task_registry is None:
            return lambda: None

        def on_task(task: Task) -> None:
            callback(
                {
                    "type": "task",
                    "id": task.id,
                    "kind": task.kind,
                    "status": task.status,
                    "label": task.label,
                    "returncode": task.returncode,
                }
            )

        return self.coder_task_registry.add_listener(on_task)

    def run(self) -> None:
        """Run the interaction loop until interrupted with Ctrl-C."""
        if self._settings.input_mode == "ptt":
            trigger = "push-to-talk (press Enter)"
        elif self._settings.wake_detector == "openwakeword":
            trigger = f'hands-free (say "{self._settings.wake_model.replace("_", " ")}")'
        else:
            trigger = f'hands-free (say "{self._settings.wake_phrase}, …")'
        print("=" * 60)
        print(" Jack — orchestrator + guarded tools")
        print(f" STT: {self._settings.stt_model}   LLM: {self._settings.llm_model}")
        print(f" Input: {trigger}")
        print(f" Workspace: {self._settings.sandbox_dir}")
        print(' Try: "create a file notes.txt", "delete notes.txt"')
        print(" Ctrl-C to quit")
        print("=" * 60)
        while True:
            try:
                if self._mode == "chat":
                    # Chat mode: don't capture from the mic — typed turns arrive via
                    # run_text_turn (driven by the daemon). The mic is released on a
                    # debounce timer (see mark_settings_changed), so a quick toggle back
                    # to voice keeps it alive. Just idle until switched back.
                    time.sleep(0.2)
                    continue
                self.run_once()
            except KeyboardInterrupt:
                print("\nBye.")
                self._transcript.close()
                return
            except Exception as exc:  # keep the loop alive on unexpected failures
                _log.exception("turn failed error=%s", exc)
                self._transcript.note(f"ERROR: {exc}")
                print(f"[error] {exc}  (see log for details)")
                # Let the user know without dumping a traceback at them — and be
                # specific when the model backend is simply unreachable.
                try:
                    self._tts.speak(
                        self._llm_unavailable_message(exc) or "Sorry, something went wrong."
                    )
                except Exception:  # never let error-handling raise
                    _log.exception("tts failed while reporting an error")
                # Recover back to a clean idle state for the next turn.
                if self._sm.can_transition(State.ERROR):
                    self._sm.transition(State.ERROR)
                self._sm.transition(State.IDLE)
