"""The durable, global, append-only usage ledger — one JSON row per finalized turn.

Lives at ``~/.autobot/usage.jsonl`` (override via ``Settings.usage_ledger_path``). Rows are
tagged with ``provider``/``model``/``workspace``/``session_id`` so every grouped view derives
from one file. All I/O is **best-effort**: ``append`` never raises (a recording failure must
not crash a turn), and ``read`` skips corrupt/partial lines (a torn final line after a crash).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from autobot.logging_setup import get_logger

_log = get_logger("usage")

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class UsageEntry:
    """One recorded turn's tokens + resolved cost.

    ``in_tokens``/``out_tokens`` serialize to the short keys ``in``/``out`` (``in`` is a
    Python keyword).
    """

    ts: str  # ISO-8601 UTC, e.g. "2026-07-14T16:02:37Z"
    provider: str
    model: str
    workspace: str
    session_id: str
    in_tokens: int
    out_tokens: int
    cache_read: int
    cache_write: int
    usd: float | None
    priced: bool

    def to_row(self) -> dict[str, Any]:
        """A JSON-serializable row (disk keys)."""
        return {
            "v": _SCHEMA_VERSION,
            "ts": self.ts,
            "provider": self.provider,
            "model": self.model,
            "workspace": self.workspace,
            "session_id": self.session_id,
            "in": self.in_tokens,
            "out": self.out_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "usd": self.usd,
            "priced": self.priced,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UsageEntry:
        """Parse a disk row (tolerant of missing optional keys)."""
        return cls(
            ts=str(row.get("ts", "")),
            provider=str(row.get("provider", "")),
            model=str(row.get("model", "")),
            workspace=str(row.get("workspace", "")),
            session_id=str(row.get("session_id", "")),
            in_tokens=int(row.get("in", 0)),
            out_tokens=int(row.get("out", 0)),
            cache_read=int(row.get("cache_read", 0)),
            cache_write=int(row.get("cache_write", 0)),
            usd=(None if row.get("usd") is None else float(row["usd"])),
            priced=bool(row.get("priced", False)),
        )


def default_path() -> Path:
    """The ledger path: ``Settings.usage_ledger_path`` if set, else ``~/.autobot/usage.jsonl``."""
    try:
        from autobot.config import Settings

        configured = Settings.load().usage_ledger_path
    except Exception:  # config is best-effort here
        configured = ""
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".autobot" / "usage.jsonl"


def append(entry: UsageEntry, *, path: Path | None = None) -> None:
    """Append one row as a JSON line. Best-effort: logs and swallows any failure."""
    target = path or default_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_row()) + "\n")
    except OSError as exc:
        _log.warning("usage ledger append failed path=%s err=%s", target, exc)


def read(*, path: Path | None = None, since: datetime | None = None) -> list[UsageEntry]:
    """All entries (oldest first), skipping corrupt lines. ``since`` filters by ``ts``.

    ``since`` may be naive (treated as UTC) or aware; comparison is done in UTC.
    """
    target = path or default_path()
    if not target.exists():
        return []
    cutoff = _iso(since) if since is not None else None
    out: list[UsageEntry] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("usage ledger read failed path=%s err=%s", target, exc)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # torn/partial line — skip, don't fail the whole read
        if not isinstance(row, dict):
            continue
        entry = UsageEntry.from_row(row)
        if cutoff is not None and entry.ts < cutoff:
            continue
        out.append(entry)
    return out


def _iso(dt: datetime) -> str:
    """A UTC ISO string comparable to stored ``ts`` values (``…Z``)."""
    from datetime import timezone

    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
