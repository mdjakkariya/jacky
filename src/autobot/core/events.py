"""The engine→UI event seam: orb states and a thread-safe event bus.

UIs (the floating orb, the terminal client) are *thin clients* of the headless
engine — they render what the engine is doing and never hold logic. This module
is the narrow, dependency-free contract between the two:

* :class:`OrbState` — the four states a UI shows, mapped from the orchestrator's
  richer internal :class:`~autobot.core.types.State` by :func:`orb_state_for`.
* :class:`EventBus` — a tiny, thread-safe publish/subscribe hub. The orchestrator
  (running on its own thread) publishes; the daemon (on the asyncio loop)
  subscribes and forwards frames to connected clients.

Keeping this pure and import-light means it is fully unit-testable without a
mic, a model, or the web framework — the bus has no idea a WebSocket exists.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from autobot.core.types import State


class OrbState(enum.Enum):
    """What the assistant is doing, in the vocabulary a UI renders.

    Deliberately coarser than the orchestrator's :class:`~autobot.core.types.State`:
    a user only needs to feel *idle / listening / thinking / talking*. The richer
    internal states collapse onto these via :func:`orb_state_for`.
    """

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    TALKING = "talking"


# Every internal State maps onto exactly one OrbState. A dict (not a function
# body) so the mapping is explicit and a test can assert it is exhaustive.
#
# Key UX decision: in hands-free mode the engine is almost always in LISTENING /
# TRANSCRIBING (it keeps the mic open and transcribes every phrase, then decides
# from the text whether it was addressed). Surfacing that as the orb's
# "listening" makes the orb never rest. So passive capture/transcribe map to
# IDLE — the orb only comes alive once the assistant is genuinely engaged with an
# addressed turn (PLANNING/EXECUTING → thinking, RESPONDING → talking). The
# OrbState.LISTENING cue is reserved for an explicit wake signal (e.g. an
# acoustic wake word), which a detector can publish directly when added.
_STATE_TO_ORB: dict[State, OrbState] = {
    State.IDLE: OrbState.IDLE,
    State.LISTENING: OrbState.IDLE,
    State.TRANSCRIBING: OrbState.IDLE,
    State.PLANNING: OrbState.THINKING,
    State.EXECUTING: OrbState.THINKING,
    State.RESPONDING: OrbState.TALKING,
    State.CLARIFYING: OrbState.TALKING,
    # Errors are transient and recover straight to idle; show the calm state
    # rather than flash a misleading cue.
    State.ERROR: OrbState.IDLE,
}


def orb_state_for(state: State) -> OrbState:
    """Map an orchestrator :class:`State` to the :class:`OrbState` a UI shows."""
    return _STATE_TO_ORB[state]


@dataclass(frozen=True, slots=True)
class StateEvent:
    """The assistant entered a new orb state."""

    state: OrbState

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "state", "value": self.state.value}


@dataclass(frozen=True, slots=True)
class VisibilityEvent:
    """A request for the UI to show or hide itself (e.g. a voice 'go away').

    Distinct from a state: it doesn't describe what the assistant is *doing*, it
    asks the orb to tuck away or come back. Showing again is normally automatic
    (the wake word re-engages the assistant), so this is mostly used to hide.
    """

    visible: bool

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "visibility", "value": "show" if self.visible else "hide"}


@dataclass(frozen=True, slots=True)
class ConfirmEvent:
    """The assistant needs the user to approve an action (shows a card).

    ``chat`` marks a confirmation raised during a typed turn: the chat drawer shows
    the card, and the voice orb ignores it (so it doesn't pop up a duplicate card
    over the drawer). ``kind`` tiers the card's tone so it's proportional to what's
    asked — ``"read"`` (calm, e.g. file-access grant), ``"write"`` (moderate), or
    ``"danger"`` (destructive). The UI styles icon/colour/wording from it.
    """

    prompt: str
    chat: bool = False
    kind: str = "danger"
    options: list[dict[str, str]] | None = None

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {
            "type": "confirm",
            "text": self.prompt,
            "mode": "chat" if self.chat else "voice",
            "kind": self.kind,
            "options": self.options,
        }


@dataclass(frozen=True, slots=True)
class ConfirmClearEvent:
    """A pending confirmation was resolved/cancelled (hide the card)."""

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "confirm_clear"}


@dataclass(frozen=True, slots=True)
class AmplitudeEvent:
    """A normalized loudness sample (0..1), meaningful while listening/talking.

    Drives the orb's reactive motion: mic RMS when listening, TTS output RMS
    when talking. A scalar only — never audio — so nothing leaves the device.
    """

    value: float

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "amplitude", "value": self.value}


@dataclass(frozen=True, slots=True)
class ContextEvent:
    """Per-session context-window usage, for the chat meter.

    ``used`` is the real prompt size (input + cached tokens), ``window`` the model's
    limit. ``price`` is the estimated USD cost of this session (cloud only); ``None``
    for local models or when the cloud model has no list price, so the UI hides the
    cost row. ``dev`` gates the detailed breakdown to dev builds. Sent after each turn.
    """

    used: int
    window: int
    dev: bool = False
    model: str = ""
    cache_read: int | None = None
    cache_write: int | None = None
    turn_in: int = 0
    turn_out: int = 0
    price: float | None = None

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        pct = round(100 * self.used / self.window) if self.window else 0
        return {
            "type": "context",
            "used": self.used,
            "window": self.window,
            "pct": pct,
            "dev": self.dev,
            "model": self.model,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "turn_in": self.turn_in,
            "turn_out": self.turn_out,
            "price": self.price,
        }


@dataclass(frozen=True, slots=True)
class ChoicesEvent:
    """Selectable items the user can act on from the chat drawer.

    Generic on purpose so *any* tool can offer follow-up choices, not just file
    search. Each item carries a label, an optional sublabel, and one or more
    ``actions``; an action either runs a registered tool (``tool`` + ``args``,
    executed through the permission gate) or copies a value client-side (``copy``).

    ``chat`` marks it as belonging to the chat drawer (the only surface that renders
    it); the voice orb ignores it, since voice acts on what the user says instead.
    """

    title: str
    items: list[dict[str, Any]]
    chat: bool = True

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {
            "type": "choices",
            "title": self.title,
            "items": self.items,
            "mode": "chat" if self.chat else "voice",
        }


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One tool step within a turn — for the chat drawer's live progress trace.

    ``status`` is ``"running"`` (emitted before the tool runs), then ``"done"`` or
    ``"failed"`` once the gate returns. ``index`` is the step's position in the
    current turn (0-based) so a client can update the same row in place.
    """

    index: int
    tool: str
    label: str
    status: str

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {
            "type": "step",
            "index": self.index,
            "tool": self.tool,
            "label": self.label,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceEvent:
    """The active folder (cwd) — for the chat drawer's folder chip."""

    path: str
    name: str

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "workspace", "path": self.path, "name": self.name}


@dataclass(frozen=True, slots=True)
class MeetingEvent:
    """Meeting recording state, for the orb's record indicator + timer."""

    state: str  # idle | recording | paused | transcribing | summarizing | done
    elapsed_s: float
    recorded_s: float
    mic_only: bool
    paused: bool
    title: str

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {
            "type": "meeting",
            "state": self.state,
            "elapsed_s": self.elapsed_s,
            "recorded_s": self.recorded_s,
            "mic_only": self.mic_only,
            "paused": self.paused,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class VoiceDownloadEvent:
    """Progress of the on-demand voice-model download (drives the Settings bar).

    ``fraction`` is 0..1 overall; ``stage`` is a short human label ("Downloading
    voice…", "Ready"); ``done`` flips true on the final frame, and ``error`` carries
    a short message if the download failed.
    """

    fraction: float
    stage: str
    done: bool = False
    error: str = ""

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        clamped = 0.0 if self.fraction < 0 else 1.0 if self.fraction > 1 else self.fraction
        pct = round(100 * clamped)
        return {
            "type": "voice_download",
            "fraction": self.fraction,
            "pct": pct,
            "stage": self.stage,
            "done": self.done,
            "error": self.error,
        }


Subscriber = Callable[[dict[str, object]], None]
"""Called with each event's wire message. Must not block (drop/queue instead)."""

AmplitudeSink = Callable[[float], None]
"""Receives a normalized loudness sample (0..1). ``EventBus.publish_amplitude``
satisfies this; components (mic capture, TTS) call it so the orb reacts live."""

VisibilitySink = Callable[[bool], None]
"""Receives a UI show/hide request (True=show, False=hide). The orb's ``dismiss``
tool calls it with ``False``; ``EventBus.publish_visibility`` satisfies it."""

ChoicesSink = Callable[[str, list[dict[str, Any]]], None]
"""Receives (title, items) to offer the user selectable actions in the chat drawer.
A tool calls it to surface follow-up choices; ``EventBus.publish_choices`` satisfies
it. See :class:`ChoicesEvent` for the item shape."""


class EventBus:
    """Thread-safe fan-out of engine events to any number of subscribers.

    The orchestrator publishes from its own thread; subscribers (e.g. the daemon's
    per-connection forwarder) receive the wire message. Subscriber callbacks must
    not block — the daemon's hands each frame to an asyncio queue and returns.

    The most recent state is remembered so a client that connects mid-session can
    be shown the current state immediately (see :attr:`last_state`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        self._last_state: OrbState = OrbState.IDLE
        self._last_workspace: dict[str, object] | None = None

    @property
    def last_state(self) -> OrbState:
        """The most recently published state (starts at ``IDLE``)."""
        with self._lock:
            return self._last_state

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register ``callback`` and return a function that unsubscribes it."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish_state(self, state: OrbState) -> None:
        """Record and broadcast a state change."""
        with self._lock:
            self._last_state = state
        self._emit(StateEvent(state).message())

    def publish_amplitude(self, value: float) -> None:
        """Broadcast a loudness sample, clamped to ``0.0..1.0``."""
        clamped = 0.0 if value < 0.0 else 1.0 if value > 1.0 else value
        self._emit(AmplitudeEvent(clamped).message())

    def publish_visibility(self, visible: bool) -> None:
        """Broadcast a UI show/hide request (does not change ``last_state``)."""
        self._emit(VisibilityEvent(visible).message())

    def publish_confirm(
        self,
        prompt: str,
        chat: bool = False,
        kind: str = "danger",
        options: list[dict[str, str]] | None = None,
    ) -> None:
        """Broadcast that a confirmation is pending (the orb shows a card).

        ``chat=True`` marks it as belonging to the chat drawer so the voice orb
        ignores it. ``kind`` tiers the card's tone ("read"/"write"/"danger");
        ``options`` (e.g. access levels) render a dropdown the user picks from.
        """
        self._emit(ConfirmEvent(prompt, chat=chat, kind=kind, options=options).message())

    def publish_confirm_clear(self) -> None:
        """Broadcast that the pending confirmation was resolved (hide the card)."""
        self._emit(ConfirmClearEvent().message())

    def publish_context(self, info: dict[str, Any], dev: bool = False) -> None:
        """Broadcast this turn's context-window usage (drives the chat meter).

        ``info`` is the model's :meth:`context_usage` payload: used, window, model,
        (cloud only) cache_read / cache_write, and (cloud only) price.
        """
        price = info.get("price")
        self._emit(
            ContextEvent(
                used=int(info.get("used", 0) or 0),
                window=int(info.get("window", 0) or 0),
                dev=dev,
                model=str(info.get("model", "") or ""),
                cache_read=info.get("cache_read"),
                cache_write=info.get("cache_write"),
                turn_in=int(info.get("turn_in", 0) or 0),
                turn_out=int(info.get("turn_out", 0) or 0),
                price=float(price) if price is not None else None,
            ).message()
        )

    def publish_voice_download(
        self, fraction: float, stage: str, done: bool = False, error: str = ""
    ) -> None:
        """Broadcast voice-download progress (the Settings view renders a bar)."""
        self._emit(VoiceDownloadEvent(fraction, stage, done=done, error=error).message())

    def publish_choices(self, title: str, items: list[dict[str, Any]], chat: bool = True) -> None:
        """Broadcast a set of selectable actions for the chat drawer to render."""
        self._emit(ChoicesEvent(title, items, chat=chat).message())

    def publish_step(self, index: int, tool: str, label: str, status: str) -> None:
        """Broadcast a tool-step update (running/done/failed) for the chat trace."""
        self._emit(StepEvent(index, tool, label, status).message())

    @property
    def last_workspace(self) -> dict[str, object] | None:
        """The most recently published workspace frame, or None."""
        with self._lock:
            return self._last_workspace

    def publish_workspace(self, path: str, name: str) -> None:
        """Record and broadcast the active folder (drives the chat folder chip)."""
        msg = WorkspaceEvent(path, name).message()
        with self._lock:
            self._last_workspace = msg
        self._emit(msg)

    def publish_meeting(self, status: dict[str, object]) -> None:
        """Broadcast a meeting status frame (the recorder's ``status()`` dict)."""
        self._emit(
            MeetingEvent(
                state=str(status.get("state", "idle")),
                elapsed_s=float(status.get("elapsed_s", 0.0)),  # type: ignore[arg-type]
                recorded_s=float(status.get("recorded_s", 0.0)),  # type: ignore[arg-type]
                mic_only=bool(status.get("mic_only", False)),
                paused=bool(status.get("paused", False)),
                title=str(status.get("title", "")),
            ).message()
        )

    def publish_mcp(self, payload: dict[str, object]) -> None:
        """Forward an MCP status or auth event to all WebSocket clients.

        A typed passthrough onto :meth:`_emit`. The worker already builds the
        final wire dict (``{"type": "mcp_status", ...}`` for state changes,
        ``{"type": "mcp_oauth", ...}`` for Phase 6 auth flows) — this method
        just routes it through the fan-out without modification.

        Args:
            payload: The wire dict produced by :class:`~autobot.mcp.session.McpServerWorker`
                or the OAuth handler; must contain at least ``{"type": str}``.
        """
        self._emit(payload)

    def _emit(self, message: dict[str, object]) -> None:
        with self._lock:
            targets = tuple(self._subscribers)
        for cb in targets:
            cb(message)
