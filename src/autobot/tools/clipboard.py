"""Clipboard tools — read and set the macOS clipboard (``pbpaste`` / ``pbcopy``).

On-device and instant. ``read_clipboard`` is ``READ_ONLY``; ``set_clipboard`` is
``WRITE`` (it replaces the clipboard contents, so it runs unprompted but audited).
A ``Runner`` is injected so the command-building is unit-tested without touching the
real clipboard, and reads are size-bounded so a huge paste can't flood the model.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# (argv, stdin) -> (returncode, output). Injectable so tests don't touch the OS.
Runner = Callable[[list[str], str | None], tuple[int, str]]

_MAX_READ = 20_000  # cap how much clipboard text we hand back to the model


def _subprocess_runner(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True)
    out = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout or "")
    return proc.returncode, out


def read_clipboard(runner: Runner | None = None) -> str:
    """Return the current clipboard text (bounded), or a friendly note if empty."""
    run = runner or _subprocess_runner
    rc, out = run(["pbpaste"], None)
    if rc != 0:
        return f"I couldn't read the clipboard: {out.strip() or 'unknown error'}"
    if not out.strip():
        return "The clipboard is empty."
    text = out if len(out) <= _MAX_READ else out[:_MAX_READ] + "…"
    _log.info("read_clipboard chars=%d", len(out))
    return f"Clipboard contents:\n{text}"


def set_clipboard(text: str, runner: Runner | None = None) -> str:
    """Put ``text`` on the clipboard so the user can paste it."""
    run = runner or _subprocess_runner
    value = text or ""
    rc, out = run(["pbcopy"], value)
    if rc != 0:
        return f"I couldn't set the clipboard: {out.strip() or 'unknown error'}"
    n = len(value)
    _log.info("set_clipboard chars=%d", n)
    return f"Copied to your clipboard ({n} character{'s' if n != 1 else ''})."


def register_clipboard_tools(registry: ToolRegistry, runner: Runner | None = None) -> None:
    """Register ``read_clipboard`` (read-only) and ``set_clipboard`` (write)."""
    registry.register(
        ToolSpec(
            name="read_clipboard",
            description=(
                "Read what's on the macOS clipboard (what the user last copied) so you can "
                "answer about it or transform it. Cues: 'what's on my clipboard', 'read my "
                "clipboard'. Also the start of the copy->ask->paste loop: when the user "
                "refers to something they just copied — 'summarize / translate / explain / "
                "fix / rewrite / reformat / clean up this' or '...what I copied / the copied "
                "text' — call read_clipboard FIRST to get the text, then do the "
                "transformation yourself and reply with the result."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: read_clipboard(runner),
            risk=Risk.READ_ONLY,
            ack="Checking your clipboard.",
        )
    )
    registry.register(
        ToolSpec(
            name="set_clipboard",
            description=(
                "Put text on the macOS clipboard so the user can paste it. Cues: 'copy "
                "this', 'copy X to my clipboard', 'put X on my clipboard'. Also finishes the "
                "copy->ask->paste loop: when the user wants a transformed result placed back "
                "— '...and copy it', 'copy that back', 'replace what I copied', 'put the "
                "translation on my clipboard' — call set_clipboard with the new text. Pass "
                "the exact text to copy as `text`."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to put on the clipboard."}
                },
                "required": ["text"],
            },
            handler=lambda text: set_clipboard(text, runner),
            risk=Risk.WRITE,
            ack="Copying that.",
        )
    )
    _log.info("clipboard tools registered (read/set)")
