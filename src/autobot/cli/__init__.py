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
    list_daemons,
    run_coder_turn,
    stop_all_daemons,
    stop_workspace,
    stream_answer,
    stream_turn,
    workspace_port,
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
    "list_daemons",
    "main",
    "resolve_workspace",
    "run_coder_turn",
    "stop_all_daemons",
    "stop_workspace",
    "stream_answer",
    "stream_turn",
    "workspace_port",
]


def resolve_workspace(cwd: Path, arg: str | None) -> Path:
    """The coder workspace: an explicit ``--workspace`` wins, else the launch cwd."""
    return (Path(arg).expanduser() if arg else cwd).resolve()


def _trust_workspace(ws: Path) -> None:
    """Mark ``ws`` trusted and create its ``.jack/`` home."""
    from autobot.trust import add_trust

    add_trust(ws)
    (ws / ".jack").mkdir(parents=True, exist_ok=True)


def _ensure_trusted(ws: Path) -> bool:
    """Prompt once to trust ``ws`` (persisting on yes). Return True if it's OK to proceed.

    An untrusted folder is never acted in: interactively we ask; non-interactively we refuse
    with a hint to run ``jack trust`` (so a piped/CI run can't silently act in an untrusted
    directory).
    """
    from autobot.trust import is_trusted

    if is_trusted(ws):
        return True
    if not sys.stdin.isatty():
        print(
            f"{ws} is not a trusted workspace. Run `jack trust` here to let Jack read, "
            "write, and run commands in it.",
            file=sys.stderr,
        )
        return False
    answer = input(f"Trust this folder? Jack can read, write, and run commands in\n  {ws}\n[y/N] ")
    if answer.strip().lower().startswith("y"):
        _trust_workspace(ws)
        return True
    print("Not trusted — aborting.", file=sys.stderr)
    return False


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
    """Build real dependencies and dispatch a ``jack config`` action.

    Writes target the current workspace's ``.jack/settings.json`` by default; ``--global``
    targets ``~/.autobot/settings.json``. Reads show the merged (workspace-over-global) view.
    """
    from getpass import getpass

    from autobot.cli.config_cmd import Deps, run
    from autobot.config import DEFAULT_SETTINGS_PATH
    from autobot.secrets import delete_secret, get_secret, set_secret

    use_global = "--global" in rest
    rest = [a for a in rest if a != "--global"]
    workspace_settings = resolve_workspace(Path.cwd(), None) / ".jack" / "settings.json"
    global_path = Path(DEFAULT_SETTINGS_PATH).expanduser()
    deps = Deps(
        settings_path=global_path if use_global else workspace_settings,
        global_path=global_path,
        workspace_settings=workspace_settings,
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
    if argv and argv[0] in ("--version", "-V", "version"):
        from autobot import __version__

        print(f"jack {__version__}")
        return 0
    if argv and argv[0] == "serve":
        from autobot.daemon.__main__ import main as daemon_main

        daemon_main(argv[1:])  # run the headless daemon (used by the frozen re-exec)
        return 0
    if argv and argv[0] == "config":
        action = argv[1] if len(argv) > 1 else "show"
        return _run_config(action, argv[2:], f"http://127.0.0.1:{_CODER_PORT}")
    if argv and argv[0] == "restart":
        ws = resolve_workspace(Path.cwd(), None)
        stopped = stop_workspace(str(ws))
        print(f"coder daemon stopped for {ws}." if stopped else "no coder daemon was running here.")
        return 0
    if argv and argv[0] == "daemons":
        rows = list_daemons()
        if not rows:
            print("no coder daemons running.")
        for r in rows:
            print(f"{'up  ' if r['up'] else 'dead'}  :{r['port']}  {r['workspace']}")
        return 0
    if argv and argv[0] == "stop":
        if "--all" in argv[1:]:
            print(f"stopped {stop_all_daemons()} coder daemon(s).")
        else:
            ws = resolve_workspace(Path.cwd(), None)
            stopped = stop_workspace(str(ws))
            print(f"stopped the coder daemon for {ws}." if stopped else "none running here.")
        return 0
    if argv and argv[0] == "trust":
        target = resolve_workspace(Path.cwd(), argv[1] if len(argv) > 1 else None)
        _trust_workspace(target)
        print(f"trusted: {target}")
        return 0
    parser = argparse.ArgumentParser(prog="jack", description="Jack coding agent (terminal).")
    parser.add_argument("text", nargs="*", help="a coding request; omit to open the TUI")
    parser.add_argument(
        "--port", type=int, default=None, help="coder daemon port (default: per-workspace)"
    )
    parser.add_argument("--workspace", default=None, help="workspace dir (default: cwd)")
    args = parser.parse_args(argv)
    ws = resolve_workspace(Path.cwd(), args.workspace)
    if not _ensure_trusted(ws):
        return 1
    port = args.port if args.port is not None else workspace_port(str(ws))
    base_url = f"http://127.0.0.1:{port}"
    try:
        ensure_daemon(base_url, port, workspace=str(ws))
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
