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

# Re-exported from live_region (single source) during the TUI migration; spinner.py is
# removed in the cutover task once shell.py no longer imports it. Listed in __all__ so the
# re-export is explicit under mypy's strict no_implicit_reexport.
from autobot.cli.live_region import VERBS, byline, verb_for

__all__ = ["VERBS", "byline", "frame_text", "verb_for", "with_spinner"]

_FRAME_INTERVAL_S = 0.09


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
