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
class AmplitudeEvent:
    """A normalized loudness sample (0..1), meaningful while listening/talking.

    Drives the orb's reactive motion: mic RMS when listening, TTS output RMS
    when talking. A scalar only — never audio — so nothing leaves the device.
    """

    value: float

    def message(self) -> dict[str, object]:
        """Serialize to the wire shape clients consume."""
        return {"type": "amplitude", "value": self.value}


Subscriber = Callable[[dict[str, object]], None]
"""Called with each event's wire message. Must not block (drop/queue instead)."""

AmplitudeSink = Callable[[float], None]
"""Receives a normalized loudness sample (0..1). ``EventBus.publish_amplitude``
satisfies this; components (mic capture, TTS) call it so the orb reacts live."""

VisibilitySink = Callable[[bool], None]
"""Receives a UI show/hide request (True=show, False=hide). The orb's ``dismiss``
tool calls it with ``False``; ``EventBus.publish_visibility`` satisfies it."""


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

    def _emit(self, message: dict[str, object]) -> None:
        with self._lock:
            targets = tuple(self._subscribers)
        for cb in targets:
            cb(message)
