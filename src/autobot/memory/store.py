"""SQLite-backed memory: the user's name plus learned facts about them.

One local file (``~/.autobot/memory.db``) holds a single evolving profile — a
``name`` and a set of short, deduplicated facts/preferences. :meth:`context`
renders it for injection into the model's prompt so Jack can address the user by
name and personalize; the memory tools grow it over time. Nothing leaves the
machine.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_MAX_FACTS_IN_CONTEXT = 40


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """The user's persistent profile (name + facts) in local SQLite.

    Pass ``":memory:"`` for an ephemeral store (tests).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            expanded = Path(self._path).expanduser()
            expanded.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(expanded)
        # check_same_thread=False to match the rest of the app (writes are
        # single-threaded on the orchestrator, but this avoids a spurious guard).
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- name ---------------------------------------------------------------
    def set_name(self, name: str) -> None:
        """Store (or replace) the user's name."""
        name = name.strip()
        if not name:
            return
        self._conn.execute(
            "INSERT INTO profile (key, value) VALUES ('name', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (name,),
        )
        self._conn.commit()

    def get_name(self) -> str | None:
        """Return the user's name, or ``None`` if unknown."""
        row = self._conn.execute("SELECT value FROM profile WHERE key = 'name'").fetchone()
        return row[0] if row else None

    # --- facts --------------------------------------------------------------
    def add_fact(self, content: str) -> bool:
        """Save a durable fact about the user. Returns ``False`` if already known.

        Deduplicated case-insensitively so repeated mentions don't pile up.
        """
        content = " ".join(content.split())  # normalize whitespace
        if not content:
            return False
        exists = self._conn.execute(
            "SELECT 1 FROM facts WHERE lower(content) = lower(?)", (content,)
        ).fetchone()
        if exists:
            return False
        self._conn.execute(
            "INSERT INTO facts (content, created_at) VALUES (?, ?)",
            (content, _utc_now_iso()),
        )
        self._conn.commit()
        return True

    def facts(self, limit: int = _MAX_FACTS_IN_CONTEXT) -> list[str]:
        """Return remembered facts, oldest first."""
        rows = self._conn.execute(
            "SELECT content FROM facts ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [r[0] for r in rows]

    def forget(self, query: str) -> int:
        """Delete facts containing ``query`` (case-insensitive). Returns count removed."""
        query = query.strip()
        if not query:
            return 0
        cur = self._conn.execute(
            "DELETE FROM facts WHERE lower(content) LIKE lower(?)", (f"%{query}%",)
        )
        self._conn.commit()
        return cur.rowcount

    # --- rendering ----------------------------------------------------------
    def context(self) -> str:
        """Render the profile for prompt injection.

        When the name is unknown, returns a nudge to introduce himself and ask it
        (so a first-time user is greeted like a human would). When known, returns
        the profile to personalize from.
        """
        name = self.get_name()
        facts = self.facts()
        known: list[str] = []
        if name:
            known.append(f"their name is {name}.")
        if facts:
            known.append("facts you've learned: " + "; ".join(facts) + ".")
        msg = ""
        if known:
            msg = (
                "What you know about the user: "
                + " ".join(known)
                + " Use this to address them by name and personalize naturally; "
                "never recite it back to them."
            )
        if not name:
            ask = (
                "You don't know the user's name yet. Early in the conversation, warmly "
                "introduce yourself in one short sentence — a friendly assistant who helps "
                "them get things done on their Mac — and ask their name; when they give it, "
                "save it with set_name. Ask just once, and still help with whatever they "
                "asked."
            )
            msg = f"{msg} {ask}".strip()
        return msg

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
