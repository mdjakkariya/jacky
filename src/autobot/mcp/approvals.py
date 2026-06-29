"""Persistence for approved tool fingerprints and spawn approvals.

All data lives in ``~/.autobot/mcp/approved.json`` (0600). Tokens are NOT here
(they're in the Keychain). This file tracks what the user has explicitly consented
to, so a rug-pull (silently changed tool definition) or a new spawn are surfaced
rather than silently executed.

Schema::

    {
        "fingerprints": {"<server_id>": {"<namespaced_tool>": "<sha256>"}},
        "spawn_approvals": {"<server_id>": {"command": "...", "args": [...], "approved_at": "..."}},
    }
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_APPROVALS_PATH = "~/.autobot/mcp/approved.json"


@dataclass
class SpawnApproval:
    """A user-approved command + args for a stdio server spawn."""

    command: str
    args: list[str]
    approved_at: str


@dataclass
class ApprovalsFile:
    """In-memory view of approved.json."""

    fingerprints: dict[str, dict[str, str]] = field(default_factory=dict)
    spawn_approvals: dict[str, SpawnApproval] = field(default_factory=dict)


def load_approvals(path: str | Path = DEFAULT_APPROVALS_PATH) -> ApprovalsFile:
    """Load approved.json; return empty ApprovalsFile on missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return ApprovalsFile()
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ApprovalsFile()
    fps: dict[str, dict[str, str]] = {}
    for sid, tools in (data.get("fingerprints") or {}).items():
        if isinstance(tools, dict):
            fps[str(sid)] = {str(k): str(v) for k, v in tools.items()}
    spawns: dict[str, SpawnApproval] = {}
    for sid, rec in (data.get("spawn_approvals") or {}).items():
        if isinstance(rec, dict) and rec.get("command"):
            spawns[str(sid)] = SpawnApproval(
                command=str(rec["command"]),
                args=[str(a) for a in (rec.get("args") or [])],
                approved_at=str(rec.get("approved_at", "")),
            )
    return ApprovalsFile(fingerprints=fps, spawn_approvals=spawns)


def save_approvals(af: ApprovalsFile, path: str | Path = DEFAULT_APPROVALS_PATH) -> None:
    """Persist ApprovalsFile to approved.json (0600)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "fingerprints": af.fingerprints,
        "spawn_approvals": {
            sid: {
                "command": sp.command,
                "args": sp.args,
                "approved_at": sp.approved_at,
            }
            for sid, sp in af.spawn_approvals.items()
        },
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(OSError):
        p.chmod(0o600)


def record_fingerprints(
    server_id: str,
    tool_fingerprints: dict[str, str],
    path: str | Path = DEFAULT_APPROVALS_PATH,
) -> None:
    """Merge ``tool_fingerprints`` into approved.json for ``server_id``."""
    af = load_approvals(path)
    af.fingerprints.setdefault(server_id, {}).update(tool_fingerprints)
    save_approvals(af, path)


def record_spawn_approval(
    server_id: str,
    command: str,
    args: list[str],
    path: str | Path = DEFAULT_APPROVALS_PATH,
) -> None:
    """Mark a stdio spawn as approved (idempotent — overwrites on re-approval)."""
    af = load_approvals(path)
    af.spawn_approvals[server_id] = SpawnApproval(
        command=command,
        args=args,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    save_approvals(af, path)
