"""Wire the engine and the daemon together, then serve.

The orchestrator runs the interaction loop on a background thread and publishes
state transitions to an :class:`~autobot.core.events.EventBus`; the daemon serves
that bus over a localhost WebSocket on the main thread. A ``--demo`` path cycles
the states with no mic or model, so the orb UI can be developed against a live
socket before the full engine is wired in.
"""

from __future__ import annotations

import math
import threading
import time

from autobot.config import Settings
from autobot.core.events import AmplitudeSink, EventBus, OrbState, orb_state_for
from autobot.core.types import State
from autobot.daemon.server import run_daemon
from autobot.logging_setup import get_logger
from autobot.orchestrator.state_machine import StateListener, _print_transition

_log = get_logger("daemon")


def make_state_listener(bus: EventBus) -> StateListener:
    """Adapt the engine's transition callback to publish onto the bus.

    Keeps the existing console line (so terminal runs are unchanged) and also
    forwards the mapped :class:`OrbState` to every connected UI client.
    """

    def listener(old: State, new: State) -> None:
        _print_transition(old, new)
        bus.publish_state(orb_state_for(new))

    return listener


def make_amplitude_sink(bus: EventBus) -> AmplitudeSink:
    """Publish loudness only while the orb is *talking*.

    The mic emits a frame ~30 times a second the whole time the engine is listening
    (which, hands-free, is almost always) — but that maps to the orb's idle state,
    so forwarding it would be constant noise on the wire for no visible effect.
    Gating on the current orb state keeps the stream quiet at rest and reactive
    only when Jack is speaking (TTS amplitude).
    """

    def sink(level: float) -> None:
        if bus.last_state is OrbState.TALKING:
            bus.publish_amplitude(level)

    return sink


def serve(settings: Settings | None = None) -> None:
    """Run the real engine behind the daemon (blocking).

    Building the orchestrator loads the STT model, the audit log, the sandbox,
    and connects to Ollama — same as a normal run, just with a UI-facing socket.
    """
    settings = settings or Settings.load()
    from autobot.app import build

    bus = EventBus()
    orchestrator = build(
        settings,
        on_state=make_state_listener(bus),
        amplitude_sink=make_amplitude_sink(bus),
        on_visibility=bus.publish_visibility,
    )
    thread = threading.Thread(target=orchestrator.run, name="engine", daemon=True)
    thread.start()
    print(f"[daemon] serving on ws://{settings.daemon_host}:{settings.daemon_port}/ws")
    # Live-apply settings/key changes from the Settings view (next turn, no restart).
    run_daemon(
        bus,
        settings.daemon_host,
        settings.daemon_port,
        on_change=orchestrator.mark_llm_dirty,
    )


def serve_demo(settings: Settings | None = None) -> None:
    """Serve a scripted state cycle with no engine — for developing the UI.

    Walks idle → listening → thinking → talking on a timer and emits a synthetic
    amplitude envelope, so the orb can be wired to a live socket without a mic,
    a model, or Ollama running.
    """
    settings = settings or Settings.load()
    bus = EventBus()

    def cycle() -> None:
        order = [OrbState.IDLE, OrbState.LISTENING, OrbState.THINKING, OrbState.TALKING]
        i = 0
        start = time.perf_counter()
        while True:
            state = order[i % len(order)]
            bus.publish_state(state)
            _log.info("demo state=%s", state.value)
            # Emit a synthetic amplitude envelope for the reactive states.
            hold = 3.0
            step = 0.05
            elapsed = 0.0
            while elapsed < hold:
                if state in {OrbState.LISTENING, OrbState.TALKING}:
                    t = time.perf_counter() - start
                    amp = abs(math.sin(t * 3.0)) * 0.6 + abs(math.sin(t * 7.3)) * 0.4
                    bus.publish_amplitude(min(1.0, amp))
                time.sleep(step)
                elapsed += step
            i += 1

    threading.Thread(target=cycle, name="demo-cycle", daemon=True).start()
    print(f"[daemon] DEMO serving on ws://{settings.daemon_host}:{settings.daemon_port}/ws")
    run_daemon(bus, settings.daemon_host, settings.daemon_port)
