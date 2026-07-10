"""`jack` — terminal client for the coder daemon (one-shot today; TUI in cli.tui)."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from pathlib import Path

from autobot.cli.client import (
    _CODER_PORT,
    _SPAWN_TIMEOUT_S,
    _log_tail,
    _post,
    _probe,
    _prompt_user,
    ensure_daemon,
    is_daemon_up,
    run_coder_turn,
    stop_daemon,
    stream_answer,
    stream_turn,
)

__all__ = [
    "_CODER_PORT",
    "_SPAWN_TIMEOUT_S",
    "_log_tail",
    "_post",
    "_probe",
    "_prompt_user",
    "ensure_daemon",
    "is_daemon_up",
    "main",
    "resolve_workspace",
    "run_coder_turn",
    "stop_daemon",
    "stream_answer",
    "stream_turn",
]


def resolve_workspace(cwd: Path, arg: str | None) -> Path:
    """The coder workspace: an explicit ``--workspace`` wins, else the launch cwd."""
    return (Path(arg).expanduser() if arg else cwd).resolve()


def _launch_editor(path: str) -> int:
    """Open ``path`` in ``$EDITOR`` (fallback: ``open -t`` on macOS)."""
    editor = os.environ.get("EDITOR")
    if editor:
        return subprocess.call([*editor.split(), path])
    if sys.platform == "darwin":
        return subprocess.call(["open", "-t", path])
    print("set $EDITOR to edit; the file is at:", file=sys.stderr)
    print(path, file=sys.stderr)
    return 1


def _run_config(action: str, rest: list[str], base_url: str) -> int:
    """Build real dependencies and dispatch a ``jack config`` action."""
    from getpass import getpass

    from autobot.cli.config_cmd import Deps, run
    from autobot.secrets import delete_secret, get_secret, set_secret

    deps = Deps(
        base_url=base_url,
        set_secret=set_secret,
        delete_secret=delete_secret,
        get_secret=get_secret,
        prompt_secret=getpass,
        launch_editor=_launch_editor,
    )
    return run(action, rest, deps)


def main(argv: list[str] | None = None) -> int:
    """`jack` opens the TUI; `jack "…"` runs a request; `jack config …` manages settings."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "config":
        action = argv[1] if len(argv) > 1 else "show"
        return _run_config(action, argv[2:], f"http://127.0.0.1:{_CODER_PORT}")
    if argv and argv[0] == "restart":
        stopped = stop_daemon(port=_CODER_PORT)
        print("coder daemon stopped." if stopped else "no coder daemon was running.")
        return 0
    parser = argparse.ArgumentParser(prog="jack", description="Jack coding agent (terminal).")
    parser.add_argument("text", nargs="*", help="a coding request; omit to open the TUI")
    parser.add_argument("--port", type=int, default=_CODER_PORT, help="coder daemon port")
    parser.add_argument("--workspace", default=None, help="workspace dir (default: cwd)")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"
    ws = resolve_workspace(Path.cwd(), args.workspace)
    try:
        ensure_daemon(base_url, args.port, workspace=str(ws))
        print(f"workspace: {ws}", file=sys.stderr)
        if args.text:
            print(run_coder_turn(base_url, " ".join(args.text)))
        else:
            import autobot.cli.tui as tui

            tui.run(base_url, str(ws))
    except (RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ImportError:
        print(
            "The interactive TUI needs the 'tui' extra — run `uv sync --extra tui`.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        # Best-effort: unblock a worker parked awaiting a reply; never fail on this.
        with contextlib.suppress(Exception):
            _post(f"{base_url}/coder/reply", {"value": "reject"}, 1.0)
        return 130
    return 0
