"""Daemon-backed and cwd-touching slash commands for the inline coder REPL.

`commands.py` stays pure (parse + the client-only trio). These handlers perform the
effectful commands — HTTP to the daemon, client-side git — with every collaborator
injected via :class:`Deps`, so they are unit-tested without a daemon, git, or a TTY.
``handle`` returns a rich renderable (or plain string) for the shell to print, or
``None`` when the command isn't daemon-backed (the shell then falls back to
``commands.dispatch``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from autobot.cli import client, gitdiff, render
from autobot.config import Settings
from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from rich.console import RenderableType

_log = get_logger("cli")

_DAEMON_CMDS = frozenset({"/diff", "/undo", "/model", "/autonomy", "/sessions", "/new", "/cost"})
_AUTONOMY = ("plan", "confirm", "auto")


def _open_report_default(rollups: dict[str, Any]) -> str:
    """Write + open the HTML dashboard, returning its path (real default for Deps)."""
    from datetime import datetime

    from autobot.usage.report import write_and_open

    return str(write_and_open(rollups, now=datetime.now()))


@dataclass(frozen=True, slots=True)
class Deps:
    """Injectable collaborators for the handlers (defaults are the real functions)."""

    working_diff: Callable[[str], str | None] = gitdiff.working_diff
    post_settings: Callable[[str, dict[str, Any]], dict[str, Any]] = client.post_settings
    get_models: Callable[[str], list[str]] = client.get_models
    coder_undo: Callable[[str], dict[str, Any]] = client.coder_undo
    coder_checkpoints: Callable[[str], list[dict[str, Any]]] = client.coder_checkpoints
    list_sessions: Callable[[str], list[dict[str, Any]]] = client.list_sessions
    resume_session: Callable[[str, str], dict[str, Any]] = client.resume_session
    new_session: Callable[[str], dict[str, Any]] = client.new_session
    load_settings: Callable[[], Any] = Settings.load
    get_usage: Callable[[str], dict[str, Any]] = client.get_usage
    open_report: Callable[[dict[str, Any]], str] = _open_report_default


def handle(
    name: str,
    args: str,
    *,
    base_url: str,
    cwd: str,
    width: int = 80,
    deps: Deps | None = None,
) -> RenderableType | str | None:
    """Run a daemon-backed command; return its renderable/text, or None if not one."""
    if name not in _DAEMON_CMDS:
        return None
    deps = deps or Deps()
    _log.debug("coder command name=%s", name)
    if name == "/diff":
        return _diff(cwd, width, deps)
    if name == "/undo":
        return _undo(base_url, args, deps)
    if name == "/model":
        return _model(base_url, args, deps)
    if name == "/autonomy":
        return _autonomy(base_url, args, deps)
    if name == "/sessions":
        return _sessions(base_url, args, deps)
    if name == "/new":
        return _new(base_url, deps)
    if name == "/cost":
        return _cost(args, base_url, width, deps)
    return None


def _diff(cwd: str, width: int, deps: Deps) -> RenderableType | str:
    """Render the working-tree diff, or a "no changes" message."""
    diff = deps.working_diff(cwd)
    if diff is None:
        return "No changes (or not a git repository)."
    return render.render_diff_rich(diff, width=width)


def _undo(base_url: str, args: str, deps: Deps) -> RenderableType | str:
    """List checkpoints (``list``) or revert the most recent one."""
    if args.strip() == "list":
        return render.render_checkpoints(deps.coder_checkpoints(base_url))
    res = deps.coder_undo(base_url)
    msg = str(res.get("message") or ("Reverted." if res.get("ok") else "Undo failed."))
    if res.get("ok"):
        return f"{msg}\n(Files created after the checkpoint are not removed.)"
    return msg


def _model(base_url: str, args: str, deps: Deps) -> str:
    """Show the current model, or switch it (field picked by active provider)."""
    s = deps.load_settings()
    is_anthropic = s.llm_provider == "anthropic"
    field = "anthropic_model" if is_anthropic else "llm_model"
    current = s.anthropic_model if is_anthropic else s.llm_model
    name = args.strip()
    if not name:
        lines = [f"Model: {current}  (provider: {s.llm_provider})"]
        if not is_anthropic:
            models = deps.get_models(base_url)
            if models:
                lines.append("Installed: " + ", ".join(models))
        lines.append("Use /model <name> to switch (applies next turn).")
        return "\n".join(lines)
    res = deps.post_settings(base_url, {field: name})
    if res.get("ok"):
        return f"Model → {name}  (applies next turn; starts a fresh conversation)"
    return f"Couldn't switch model: {res.get('error', 'unknown error')}"


def _autonomy(base_url: str, args: str, deps: Deps) -> str:
    """Show the current autonomy level, or set it after validating the value."""
    value = args.strip().lower()
    if not value:
        current = deps.load_settings().coding_autonomy
        return f"Autonomy: {current}\nOptions: {', '.join(_AUTONOMY)}  (use /autonomy <value>)"
    if value not in _AUTONOMY:
        return f"Unknown autonomy '{value}'. Options: {', '.join(_AUTONOMY)}."
    res = deps.post_settings(base_url, {"coding_autonomy": value})
    if res.get("ok"):
        return f"Autonomy → {value}  (applies next turn)"
    return f"Couldn't set autonomy: {res.get('error', 'unknown error')}"


def _sessions(base_url: str, args: str, deps: Deps) -> RenderableType | str:
    """Resume a session by id (``resume <id>``), or render the sessions table."""
    parts = args.split()
    if parts and parts[0] == "resume":
        if len(parts) < 2:
            return "Usage: /sessions resume <id>"
        res = deps.resume_session(base_url, parts[1])
        return f"Resumed session {parts[1]}." if res.get("ok") else "No such session."
    return render.render_sessions(deps.list_sessions(base_url))


def _new(base_url: str, deps: Deps) -> str:
    """Start a fresh session on the daemon."""
    res = deps.new_session(base_url)
    return "Started a new session." if res.get("ok") else "Couldn't start a new session."


def _cost(args: str, base_url: str, width: int, deps: Deps) -> RenderableType | str:
    """`/cost` renders the summary; `/cost open` builds + opens the browser dashboard."""
    payload = deps.get_usage(base_url)
    if args.strip() == "open":
        rollups = payload.get("rollups") if isinstance(payload, dict) else None
        if not rollups:
            return "No usage recorded yet — nothing to open."
        path = deps.open_report(rollups)
        return f"Opened the usage report ({path})."
    return render.render_cost(payload if isinstance(payload, dict) else {}, width)
