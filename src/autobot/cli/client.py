"""HTTP client for the coder daemon: readiness, spawn, and the turn drive loop.

Dependency-free: talks HTTP with ``urllib`` and spawns the daemon with ``subprocess``,
so it runs the same on Linux, macOS, and Windows.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
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


OpenStream = Callable[[str, dict[str, Any]], Iterator[str]]


def _open_stream(url: str, payload: dict[str, Any]) -> Iterator[str]:  # pragma: no cover
    """POST ``payload`` and yield decoded response lines (an SSE stream)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=600.0)
    for raw in resp:
        yield raw.decode("utf-8", errors="replace").rstrip("\n")


def _parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Parse ``data: {json}`` SSE lines into event dicts (ignoring blanks/other fields)."""
    for line in lines:
        if line.startswith("data:"):
            body = line[len("data:") :].strip()
            if not body:
                continue
            try:
                evt = json.loads(body)
            except ValueError:
                continue
            if isinstance(evt, dict):
                yield evt


def _stream(
    base_url: str, path: str, payload: dict[str, Any], open_stream: OpenStream
) -> Iterator[dict[str, Any]]:
    """Open an SSE POST and yield parsed events; transport errors become one error event."""
    try:
        yield from _parse_sse(open_stream(f"{base_url}{path}", payload))
    except (OSError, urllib.error.URLError) as exc:
        yield {"status": "error", "reply": f"I couldn't reach the coder daemon: {exc}"}


def stream_turn(
    base_url: str, text: str, *, open_stream: OpenStream = _open_stream
) -> Iterator[dict[str, Any]]:
    """Begin a coder turn; yield its SSE events."""
    return _stream(base_url, "/coder/turn", {"text": text}, open_stream)


def stream_answer(
    base_url: str, value: str, text: str = "", *, open_stream: OpenStream = _open_stream
) -> Iterator[dict[str, Any]]:
    """Answer a parked turn; yield the next phase's SSE events."""
    return _stream(base_url, "/coder/reply", {"value": value, "text": text}, open_stream)


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
    open_stream: OpenStream = _open_stream,
    prompt: Callable[[dict[str, Any]], dict[str, str]] = _prompt_user,
) -> str:
    """Drive one coding turn over the SSE stream: start, answer each plan/pending, return reply."""
    events = stream_turn(base_url, text, open_stream=open_stream)
    while True:
        phase = None
        for evt in events:
            if evt.get("status") in ("plan", "pending", "done", "error"):
                phase = evt
                break
            # token/tool events are ignored in one-shot mode
        if phase is None:
            return ""  # stream ended without a phase event
        status = phase.get("status")
        if status in ("done", "error"):
            reply = phase.get("reply")
            return reply if isinstance(reply, str) else ""
        ans = prompt(phase)
        events = stream_answer(
            base_url, ans.get("value", ""), ans.get("text", ""), open_stream=open_stream
        )


def _log_tail(path: Path, limit: int = 2000) -> str:  # pragma: no cover - trivial file read
    """The last ``limit`` chars of a log file, for surfacing a daemon startup failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return "(no output captured)"


def ensure_daemon(base_url: str, port: int = _CODER_PORT) -> None:  # pragma: no cover
    """Start a coder-profile daemon on ``port`` if one isn't already answering (spawns it).

    Raises ``RuntimeError`` (with the log tail) if the daemon exits before it answers, or
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
        if proc.poll() is not None:
            raise RuntimeError(
                f"the coder daemon couldn't start (exit {proc.returncode}). "
                f"Recent output ({log_path}):\n{_log_tail(log_path)}"
            )
        time.sleep(0.3)
    raise TimeoutError(
        f"the coder daemon didn't answer on {base_url} within {_SPAWN_TIMEOUT_S:.0f}s "
        f"(see {log_path})"
    )
