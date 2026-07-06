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
    answer,
    ensure_daemon,
    is_daemon_up,
    run_coder_turn,
    start_turn,
)

__all__ = [
    "_CODER_PORT",
    "_SPAWN_TIMEOUT_S",
    "_log_tail",
    "_post",
    "_probe",
    "_prompt_user",
    "answer",
    "ensure_daemon",
    "is_daemon_up",
    "main",
    "run_coder_turn",
    "start_turn",
]


def main(argv: list[str] | None = None) -> int:
    """`jack "…"` — send one coding request to the coder daemon and print the reply."""
    parser = argparse.ArgumentParser(
        prog="jack", description="Jack coding agent (terminal client)."
    )
    parser.add_argument("text", nargs="+", help='the coding request, e.g. jack "add a test"')
    parser.add_argument("--port", type=int, default=_CODER_PORT, help="coder daemon port")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"
    text = " ".join(args.text)
    try:
        ensure_daemon(base_url, args.port)
        print(run_coder_turn(base_url, text))
    except (RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        # Best-effort: unblock a worker parked awaiting a reply; never fail on this.
        with contextlib.suppress(Exception):
            _post(f"{base_url}/coder/reply", {"value": "reject"}, 1.0)
        return 130
    return 0
