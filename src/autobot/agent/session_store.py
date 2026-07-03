"""JSONL persistence + resume for :class:`~autobot.agent.session.Session`.

Each session is one newline-delimited JSON file under ``root/<id>.jsonl``. The
first line is a ``{"type": "meta", ...}`` header (id, cwd, model, created); every
later line is a ``{"type": "msg", "message": <provider-native message>}`` event,
appended as turns complete. This is append-only and diff-friendly, and a resume
just replays the ``msg`` lines back into ``Session.history``. Time is injected so
the logic is unit-testable and deterministic.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from autobot.agent.session import Session
from autobot.logging_setup import get_logger

_log = get_logger("session")


class SessionStore:
    """Creates, appends to, lists, and loads sessions as JSONL files."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        """A fresh session id (uuid4 hex)."""
        return uuid.uuid4().hex

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    def create(self, cwd: str, model: str) -> Session:
        """Create a new session and write its meta header line."""
        session = Session(id=self.new_id(), cwd=cwd, model=model)
        meta = {"type": "meta", "id": session.id, "cwd": cwd, "model": model}
        with self._path(session.id).open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta) + "\n")
        _log.info("session created id=%s cwd=%s model=%s", session.id, cwd, model)
        return session

    def append(self, session: Session, events: list[dict[str, Any]]) -> None:
        """Append message ``events`` (provider-native) to the session's transcript."""
        if not events:
            return
        path = self._path(session.id)
        if not path.exists():  # session created out-of-band; write a header first
            with path.open("w", encoding="utf-8") as fh:
                meta_dict = {
                    "type": "meta",
                    "id": session.id,
                    "cwd": session.cwd,
                    "model": session.model,
                }
                fh.write(json.dumps(meta_dict) + "\n")
        with path.open("a", encoding="utf-8") as fh:
            for msg in events:
                fh.write(json.dumps({"type": "msg", "message": msg}) + "\n")

    def load(self, session_id: str) -> Session | None:
        """Rebuild a session by replaying its transcript, or ``None`` if absent."""
        path = self._path(session_id)
        if not path.exists():
            return None
        meta: dict[str, Any] = {}
        history: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):  # a valid-JSON non-object line (e.g. a bare list)
                continue
            if rec.get("type") == "meta":
                meta = rec
            elif rec.get("type") == "msg" and isinstance(rec.get("message"), dict):
                history.append(rec["message"])
        session = Session(
            id=meta.get("id", session_id),
            cwd=meta.get("cwd", ""),
            model=meta.get("model", ""),
            history=history,
        )
        _log.info("session resumed id=%s messages=%d", session.id, len(history))
        return session

    def list(self) -> list[dict[str, Any]]:
        """Summaries of stored sessions (id/cwd/model/mtime), most recent first."""
        rows: list[dict[str, Any]] = []
        for path in self._root.glob("*.jsonl"):
            try:
                first = path.read_text(encoding="utf-8").splitlines()[0]
                meta = json.loads(first)
            except (OSError, IndexError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "id": meta.get("id", path.stem),
                    "cwd": meta.get("cwd", ""),
                    "model": meta.get("model", ""),
                    "mtime": path.stat().st_mtime,
                }
            )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return rows
