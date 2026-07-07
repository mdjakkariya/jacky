"""Jack's braille-orbit spinner: a threaded live region shown during a blocking call.

The glyph rotates through :data:`theme.SPINNER_FRAMES`; a per-turn verb and a width-gated
byline sit beside it. Purely cosmetic — it never logs and always tears its thread down.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.live import Live
from rich.text import Text

from autobot.cli import theme

# Jack's own verb pool — professional, with a light workshop motif fitting the name.
VERBS: tuple[str, ...] = (
    "Working",
    "Thinking",
    "Planning",
    "Tracing",
    "Reading",
    "Wiring",
    "Drafting",
    "Assembling",
    "Untangling",
    "Fitting",
    "Tightening",
    "Leveling",
    "Bracing",
    "Hoisting",
    "Rigging",
)

_FRAME_INTERVAL_S = 0.09


def verb_for(turn_index: int) -> str:
    """Pick a verb by turn index — varied across turns, deterministic (no RNG)."""
    return VERBS[turn_index % len(VERBS)]


def byline(elapsed_s: float, width: int) -> str:
    """A width-gated ``esc to interrupt · Ns`` byline (drops parts as width shrinks)."""
    secs = f"{int(elapsed_s)}s"
    full = f"esc to interrupt · {secs}"
    if width >= len(full) + 4:
        return full
    if width >= len(secs) + 4:
        return secs
    return ""


def frame_text(verb: str, frame_char: str, elapsed_s: float, width: int) -> Text:
    """Compose one spinner frame: ``<glyph> <verb>…  · byline``."""
    line = Text()
    line.append(frame_char + " ", style="teal")
    line.append(f"{verb}…", style="bold")
    tail = byline(elapsed_s, width)
    if tail:
        line.append("  ·  " + tail, style="dim")
    return line


@contextmanager
def with_spinner(console: Console, verb: str) -> Iterator[None]:
    """Run the braille-orbit spinner on a daemon thread until the context exits."""
    stop = threading.Event()
    started = time.monotonic()

    def _loop() -> None:
        width = console.width or 80
        with Live(console=console, refresh_per_second=12, transient=True) as live:
            i = 0
            while not stop.is_set():
                frame = theme.SPINNER_FRAMES[i % len(theme.SPINNER_FRAMES)]
                live.update(frame_text(verb, frame, time.monotonic() - started, width))
                i += 1
                stop.wait(_FRAME_INTERVAL_S)

    thread = threading.Thread(target=_loop, name="jack-spinner", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
