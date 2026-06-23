"""Empty the macOS Trash — a destructive, gated tool.

Permanently deletes everything in the Trash via Finder (``osascript``). It's
classified ``DESTRUCTIVE``, so the permission gate confirms before it runs (by
voice, via :class:`~autobot.tools.confirm.VoiceConfirmer`). The shell call goes
through an injectable runner so the command and messages are unit-tested without
touching the real Trash.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# (argv) -> (returncode, output). Injectable so tests don't run osascript.
Runner = Callable[[list[str]], tuple[int, str]]

# Finder actually empties the Trash (handles external-volume trashes too). We do
# NOT pre-count ~/.Trash: it's TCC-protected, so a permission error there used to
# be misread as "already empty", skipping the empty entirely. Always run it and
# report what really happened.
#
# `empty the trash` on an *already-empty* Trash raises AppleScript -128
# (userCanceledErr) — the most common cause of that error. So we count first via
# Finder and only empty when there's something there, returning the count so we can
# report "already empty" vs "emptied" honestly. We also turn off Finder's own
# "are you sure?" warning around the empty (we've already confirmed through our own
# gate) so a populated Trash doesn't pop a dialog and -128 on us either. The
# original warning setting is restored even on error. Counting is done through
# Finder (Automation) — NOT by reading ~/.Trash, which is TCC-protected.
_EMPTY_TRASH_SCRIPT = (
    'tell application "Finder"\n'
    "\tset _warn to warns before emptying of trash\n"
    "\tset warns before emptying of trash to false\n"
    "\tset _n to count of items in trash\n"
    "\tif _n > 0 then\n"
    "\t\ttry\n"
    "\t\t\tempty the trash\n"
    "\t\ton error _msg number _num\n"
    "\t\t\tset warns before emptying of trash to _warn\n"
    "\t\t\terror _msg number _num\n"
    "\t\tend try\n"
    "\tend if\n"
    "\tset warns before emptying of trash to _warn\n"
    "\treturn _n\n"
    "end tell"
)
_EMPTY_TRASH = ["osascript", "-e", _EMPTY_TRASH_SCRIPT]


def _subprocess_runner(argv: list[str]) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, (proc.stderr or proc.stdout)


def empty_trash(runner: Runner | None = None) -> str:
    """Empty the macOS Trash; returns a spoken-friendly summary of the real result."""
    run = runner or _subprocess_runner
    rc, out = run(_EMPTY_TRASH)
    if rc != 0:
        detail = out.strip() or "unknown error"
        _log.warning("empty_trash failed rc=%d out=%r", rc, out)
        # A denied Automation/Finder permission is the common cause — say so plainly.
        if "not allowed" in detail.lower() or "1743" in detail or "-1743" in detail:
            return (
                "I couldn't empty the Trash — macOS blocked me from controlling "
                "Finder. Allow it under System Settings → Privacy & Security → "
                "Automation."
            )
        # -128 = canceled (e.g. a locked item). We avoid the already-empty -128 by
        # counting first; this is the fallback for any other cancel.
        if "-128" in detail:
            return "I couldn't empty the Trash — the action was canceled, so nothing was deleted."
        return f"I couldn't empty the Trash: {detail}"
    # On success the script returns the number of items it found before emptying.
    count = out.strip()
    if count == "0":
        _log.info("empty_trash: already empty")
        return "Your Trash is already empty — nothing to delete."
    _log.info("emptied trash items=%s", count or "?")
    return "Done — I've emptied the Trash."


def register_trash_tools(registry: ToolRegistry, runner: Runner | None = None) -> None:
    """Register the ``empty_trash`` tool (DESTRUCTIVE, so the gate confirms it)."""
    registry.register(
        ToolSpec(
            name="empty_trash",
            description=(
                "Empty the macOS Trash, permanently deleting everything in it. This is "
                "destructive and cannot be undone — the user will be asked to confirm. "
                "Spoken cues: 'empty the trash', 'clean out the trash', 'take out the "
                "trash', 'empty the bin'."
            ),
            parameters={"type": "object", "properties": {}},
            handler=lambda: empty_trash(runner),
            risk=Risk.DESTRUCTIVE,
            confirm_prompt="🗑️ Empty the Trash? This permanently deletes everything in it.",
            ack="Emptying the Trash.",
        )
    )
