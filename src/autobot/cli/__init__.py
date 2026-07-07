"""`jack` — terminal client for the coder daemon (one-shot today; TUI in cli.tui)."""

from __future__ import annotations

import argparse
import contextlib
import sys

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
    "run_coder_turn",
    "stream_answer",
    "stream_turn",
]


def main(argv: list[str] | None = None) -> int:
    """`jack` opens the TUI; `jack "…"` runs one coding request and prints the reply."""
    parser = argparse.ArgumentParser(prog="jack", description="Jack coding agent (terminal).")
    parser.add_argument("text", nargs="*", help="a coding request; omit to open the TUI")
    parser.add_argument("--port", type=int, default=_CODER_PORT, help="coder daemon port")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        ensure_daemon(base_url, args.port)
        if args.text:
            print(run_coder_turn(base_url, " ".join(args.text)))
        else:
            from pathlib import Path

            import autobot.cli.tui as tui

            tui.run(base_url, str(Path.cwd()))
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
