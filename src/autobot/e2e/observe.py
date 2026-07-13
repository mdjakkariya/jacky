"""Capture the daemon-side observability files for a run's artifact bundle.

The runner drives the coder through a real daemon; these helpers gather the three
things that make a bundle debuggable on its own — the effective settings, the coder's
session transcript, and the slice of the daemon log written during the run. All are
**best-effort**: a missing or unreadable file yields ``""`` rather than raising, so a
capture failure never fails the run it was observing. Pure ``path -> str`` functions,
so they unit-test against a ``tmp_path`` with no daemon.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from autobot.config import Settings


def settings_snapshot(settings: Settings) -> str:
    """The effective settings as pretty JSON (secrets live in the Keychain, never here)."""
    try:
        return json.dumps(asdict(settings), indent=2, default=str, sort_keys=True)
    except Exception:  # a snapshot must never break the run it observes
        return ""


def session_jsonl(workspace: str | Path) -> str:
    """The newest coder session transcript under ``<workspace>/.jack/sessions``, or ``""``."""
    sessions = Path(workspace).expanduser() / ".jack" / "sessions"
    try:
        files = sorted(sessions.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    for f in files:  # newest first; skip any that vanish/aren't readable
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def log_offset(log_path: str | Path) -> int:
    """The log file's current byte size, so only what's appended after is read (0 if absent)."""
    try:
        return Path(log_path).expanduser().stat().st_size
    except OSError:
        return 0


def log_since(log_path: str | Path, offset: int) -> str:
    """The log bytes written since ``offset`` — this run's slice — or ``""`` on any failure.

    If the file has since shrunk below ``offset`` (a rotation mid-run), fall back to the
    whole current file rather than returning garbage.
    """
    try:
        data = Path(log_path).expanduser().read_bytes()
    except OSError:
        return ""
    start = offset if 0 <= offset <= len(data) else 0
    return data[start:].decode("utf-8", "replace")
