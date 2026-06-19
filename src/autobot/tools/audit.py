"""SQLite-backed audit log for the permission gate.

Every tool invocation the gate considers — allowed or denied — is recorded here.
The log is append-only from the app's perspective (we only ever insert and read),
giving a tamper-evident trail of what the assistant did and when. One local
SQLite file keeps the privacy story clean: nothing leaves the machine.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from autobot.core.types import AuditEntry, Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    tool       TEXT    NOT NULL,
    arguments  TEXT    NOT NULL,
    risk       TEXT    NOT NULL,
    decision   TEXT    NOT NULL,
    ok         INTEGER,
    detail     TEXT    NOT NULL
);
"""


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """Append and read permission-gate decisions in a local SQLite database.

    Pass ``":memory:"`` as the path for an ephemeral log (used in tests).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._path = str(Path(self._path).expanduser())
        # check_same_thread=False: the audio callback runs on a worker thread,
        # but all audit writes happen on the main thread; this just avoids a
        # spurious guard if that ever changes.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record(self, entry: AuditEntry) -> None:
        """Append one decision to the log."""
        self._conn.execute(
            "INSERT INTO audit (ts, tool, arguments, risk, decision, ok, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.timestamp,
                entry.tool,
                json.dumps(entry.arguments, default=str),
                entry.risk,
                entry.decision.value,
                None if entry.ok is None else int(entry.ok),
                entry.detail,
            ),
        )
        self._conn.commit()

    def log(
        self,
        *,
        tool: str,
        arguments: dict[str, object],
        risk: str,
        decision: Decision,
        ok: bool | None,
        detail: str,
    ) -> AuditEntry:
        """Build an :class:`AuditEntry` (timestamped now) and record it.

        Returns:
            The recorded entry, for convenience/logging.
        """
        entry = AuditEntry(
            timestamp=_utc_now_iso(),
            tool=tool,
            arguments=dict(arguments),
            risk=risk,
            decision=decision,
            ok=ok,
            detail=detail,
        )
        self.record(entry)
        return entry

    def recent(self, limit: int = 20) -> list[AuditEntry]:
        """Return the most recent entries, newest first."""
        rows = self._conn.execute(
            "SELECT ts, tool, arguments, risk, decision, ok, detail "
            "FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            AuditEntry(
                timestamp=ts,
                tool=tool,
                arguments=json.loads(arguments),
                risk=risk,
                decision=Decision(decision),
                ok=None if ok is None else bool(ok),
                detail=detail,
            )
            for (ts, tool, arguments, risk, decision, ok, detail) in rows
        ]

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
