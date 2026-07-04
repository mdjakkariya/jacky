"""`jack` — a tiny cross-platform terminal client for the coder daemon.

Sends a coding request to a warm coder-profile daemon (spawning one on first use) and
prints the reply. Dependency-free: talks HTTP with ``urllib`` and spawns the daemon with
``subprocess``, so it runs the same on Linux, macOS, and Windows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

_CODER_PORT = 8766  # coder daemon port (kept off the assistant daemon's 8765)
_SPAWN_TIMEOUT_S = 30.0


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:  # pragma: no cover
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    parsed: dict[str, Any] = json.loads(body)
    return parsed


def _probe(url: str, timeout: float) -> bool:  # pragma: no cover - real network probe
    with urllib.request.urlopen(url, timeout=timeout):
        return True


def is_daemon_up(base_url: str, probe: Callable[[str, float], bool] = _probe) -> bool:
    """True if a daemon answers a quick readiness probe at ``base_url``."""
    try:
        return probe(f"{base_url}/sessions", 1.0)
    except OSError:
        return False


def _prompt_user(resp: dict[str, Any]) -> dict[str, str]:  # pragma: no cover - terminal I/O
    """Ask the user to answer a plan or a pending command, in the real terminal."""
    if resp.get("status") == "plan":
        print("\nPLAN\n" + str(resp.get("reply", "")))
        choice = input("\nApply this plan? [y]es / [n]o / [e]dit: ").strip().lower()
        if choice in ("y", "yes"):
            return {"value": "approve"}
        if choice in ("e", "edit"):
            return {"value": "refine", "text": input("What should change? ").strip()}
        return {"value": "reject"}
    print("\n" + str(resp.get("prompt", "Proceed?")))
    return {"value": "yes" if input("[y/N] ").strip().lower() in ("y", "yes") else "no"}


def run_coder_turn(
    base_url: str,
    text: str,
    *,
    post: Callable[[str, dict[str, Any], float], dict[str, Any]] = _post,
    prompt: Callable[[dict[str, Any]], dict[str, str]] = _prompt_user,
) -> str:
    """Drive one coding turn: start, then answer each plan/pending event until done."""

    def _send(path: str, payload: dict[str, Any]) -> dict[str, Any] | str:
        try:
            return post(f"{base_url}{path}", payload, 600.0)
        except (OSError, urllib.error.URLError) as exc:
            return f"I couldn't reach the coder daemon: {exc}"
        except ValueError as exc:  # non-JSON body
            return f"The coder daemon sent a response I couldn't read: {exc}"

    resp = _send("/coder/turn", {"text": text})
    if isinstance(resp, str):
        return resp
    while resp.get("status") in ("plan", "pending"):
        resp = _send("/coder/reply", dict(prompt(resp)))
        if isinstance(resp, str):
            return resp
    reply = resp.get("reply")
    return reply if isinstance(reply, str) else ""


def _log_tail(path: Path, limit: int = 2000) -> str:  # pragma: no cover - trivial file read
    """The last ``limit`` chars of a log file, for surfacing a daemon startup failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return "(no output captured)"


def ensure_daemon(base_url: str, port: int = _CODER_PORT) -> None:  # pragma: no cover
    """Start a coder-profile daemon on ``port`` if one isn't already answering (spawns it).

    The daemon's output is logged to a file so a startup failure (e.g. the ``daemon`` extra
    isn't installed) is surfaced rather than causing a silent 30-second hang. Raises
    ``RuntimeError`` (with the log tail) if the daemon exits before it answers, or
    ``TimeoutError`` if it never answers.
    """
    if is_daemon_up(base_url):
        return
    log_path = Path.home() / ".autobot" / "logs" / "jack-coder-daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(  # fixed argv, our own module
            [sys.executable, "-m", "autobot.daemon", "--profile", "coder", "--port", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + _SPAWN_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_daemon_up(base_url):
            return
        if proc.poll() is not None:  # the daemon process exited before answering
            raise RuntimeError(
                f"the coder daemon couldn't start (exit {proc.returncode}). "
                f"Recent output ({log_path}):\n{_log_tail(log_path)}"
            )
        time.sleep(0.3)
    raise TimeoutError(
        f"the coder daemon didn't answer on {base_url} within {_SPAWN_TIMEOUT_S:.0f}s "
        f"(see {log_path})"
    )


def main(argv: list[str] | None = None) -> int:
    """`jack "…"` — send one coding request to the coder daemon and print the reply."""
    parser = argparse.ArgumentParser(
        prog="jack", description="Jack coding agent (terminal client)."
    )
    parser.add_argument(
        "text", nargs="+", help='the coding request, e.g. jack "add a test for foo"'
    )
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
        return 130
    return 0
