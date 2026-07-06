"""In-session slash commands: parse a ``/name args`` line and dispatch it.

Commands are pure — they return a :class:`CommandResult` the UI shell enacts (print a
message, clear the transcript, or exit) — so they're testable without a UI and reused by
any shell. v1 has only client-side commands; daemon-backed ones (``/undo``…) come later.
"""

from __future__ import annotations

from dataclasses import dataclass

COMMANDS: dict[str, str] = {
    "/help": "show this help",
    "/clear": "clear the transcript",
    "/exit": "quit jack",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What the UI shell should do in response to a command."""

    action: str  # "message" | "clear" | "exit"
    text: str = ""


def parse(line: str) -> tuple[str, str] | None:
    """Split a ``/``-prefixed line into ``(name, args)``; ``None`` if it isn't a command."""
    if not line.startswith("/"):
        return None
    head, _, rest = line.strip().partition(" ")
    return head, rest.strip()


def _help_text() -> str:
    return "Commands:\n" + "\n".join(f"  {name}  {desc}" for name, desc in COMMANDS.items())


def dispatch(name: str, args: str) -> CommandResult:
    """Run a client-side command by name."""
    if name == "/help":
        return CommandResult("message", _help_text())
    if name == "/clear":
        return CommandResult("clear")
    if name == "/exit":
        return CommandResult("exit")
    return CommandResult("message", f"Unknown command: {name} (try /help)")
