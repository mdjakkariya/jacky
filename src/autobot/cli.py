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


def send_chat(
    base_url: str,
    text: str,
    post: Callable[[str, dict[str, Any], float], dict[str, Any]] = _post,
) -> str:
    """POST the request to the daemon's /chat and return the reply (or a friendly error)."""
    try:
        result = post(f"{base_url}/chat", {"text": text}, 600.0)
    except (OSError, urllib.error.URLError) as exc:
        return f"I couldn't reach the coder daemon: {exc}"
    except ValueError as exc:  # non-JSON body (e.g. a stranger answering on the port)
        return f"The coder daemon sent a response I couldn't read: {exc}"
    if not result.get("ok"):
        fallback = "the coder daemon couldn't handle that."
        return result.get("error") or result.get("reply") or fallback
    reply = result.get("reply")
    return reply if isinstance(reply, str) else ""


def ensure_daemon(base_url: str, port: int = _CODER_PORT) -> None:  # pragma: no cover
    """Start a coder-profile daemon on ``port`` if one isn't already answering (spawns it)."""
    if is_daemon_up(base_url):
        return
    subprocess.Popen(  # fixed argv, our own module
        [sys.executable, "-m", "autobot.daemon", "--profile", "coder", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + _SPAWN_TIMEOUT_S
    while time.monotonic() < deadline:
        if is_daemon_up(base_url):
            return
        time.sleep(0.3)
    raise TimeoutError(f"coder daemon did not start on {base_url} within {_SPAWN_TIMEOUT_S:.0f}s")


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
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(send_chat(base_url, text))
    return 0
