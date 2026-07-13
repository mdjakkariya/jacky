"""A tiny PID file for the shared coder daemon: which PID, workspace, and port it serves.

Lets the ``jack`` CLI find the running daemon, learn what workspace it is bound to, and
stop it (for a restart or a workspace switch). All paths are injectable so it unit-tests
against a temp file.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

DEFAULT_PIDFILE = Path("~/.autobot/coder-daemon.pid").expanduser()


def write_pidfile(pid: int, workspace: str, port: int, *, path: Path = DEFAULT_PIDFILE) -> None:
    """Record the running coder daemon's pid/workspace/port."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "workspace": workspace, "port": port}), encoding="utf-8"
    )


def read_pidfile(*, path: Path = DEFAULT_PIDFILE) -> dict[str, object] | None:
    """Return the recorded pid/workspace/port, or None if missing/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_pidfile(*, path: Path = DEFAULT_PIDFILE) -> None:
    """Delete the PID file (best effort)."""
    with contextlib.suppress(OSError):
        path.unlink()
