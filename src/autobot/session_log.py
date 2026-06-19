"""Per-session transcript — a readable record of one wake→…→idle session.

Unlike the rotating debug log (terse, all components), this is a clean Markdown
file of the actual conversation plus key debug notes (tool calls, token usage,
compaction, errors). One file per run, written to the project's ``sessions/``
folder so it's easy to open, diff, and share when reviewing how a session went.

:class:`NullTranscript` is the no-op used when session logging is disabled, so
callers never branch on ``None``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class Transcript(Protocol):
    """Sink for session events; see :class:`FileTranscript` for the format."""

    def user(self, text: str, confidence: float) -> None: ...
    def assistant(self, text: str) -> None: ...
    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None: ...
    def note(self, text: str) -> None: ...
    def close(self) -> None: ...


class NullTranscript:
    """Does nothing — used when session transcripts are disabled."""

    path: Path | None = None

    def user(self, text: str, confidence: float) -> None: ...
    def assistant(self, text: str) -> None: ...
    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None: ...
    def note(self, text: str) -> None: ...
    def close(self) -> None: ...


class FileTranscript:
    """Appends a readable Markdown transcript for one session."""

    def __init__(self, directory: str | Path, header: str = "") -> None:
        started = datetime.now()
        self.path = Path(directory).expanduser() / f"session-{started:%Y%m%d-%H%M%S}.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open = self.path.open("a", encoding="utf-8")
        self._write(f"# Autobot session — {started:%Y-%m-%d %H:%M:%S}\n")
        if header:
            self._write(f"\n{header}\n")
        self._write("\n")

    def _write(self, text: str) -> None:
        self._open.write(text)
        self._open.flush()

    @staticmethod
    def _time() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def user(self, text: str, confidence: float) -> None:
        """Record what the user said."""
        self._write(f"**[{self._time()}] You** _(conf {confidence:.2f})_: {text}\n\n")

    def assistant(self, text: str) -> None:
        """Record the assistant's spoken reply."""
        self._write(f"**Autobot:** {text}\n\n")

    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None:
        """Record a tool execution and its outcome."""
        status = "ok" if ok else "FAILED"
        self._write(f"> 🔧 `{name}({arguments})` → {status}: {detail}\n\n")

    def note(self, text: str) -> None:
        """Record a debug note (tokens, compaction, errors, ignored utterances)."""
        self._write(f"> _{self._time()} · {text}_\n\n")

    def close(self) -> None:
        """Write the footer and close the file."""
        self._write(f"\n_session ended {datetime.now():%Y-%m-%d %H:%M:%S}_\n")
        self._open.close()
