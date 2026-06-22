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


def prune_sessions(directory: str | Path, keep: int) -> list[Path]:
    """Delete all but the ``keep`` most recent ``session-*.md`` files.

    Keeps the sessions folder bounded so it never accumulates hundreds of files.
    Filenames are timestamped (``session-YYYYmmdd-HHMMSS.md``), so a name sort is a
    time sort. Returns the paths that were deleted. Never raises — a cleanup failure
    must not block startup.
    """
    folder = Path(directory).expanduser()
    if keep < 0 or not folder.is_dir():
        return []
    files = sorted(folder.glob("session-*.md"))
    stale = files[:-keep] if keep else files
    deleted: list[Path] = []
    for path in stale:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            pass
    return deleted


class Transcript(Protocol):
    """Sink for session events; see :class:`FileTranscript` for the format."""

    def user(self, text: str, confidence: float) -> None: ...
    def assistant(self, text: str) -> None: ...
    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None: ...
    def note(self, text: str) -> None: ...
    def record_usage(self, in_tokens: int, out_tokens: int, cost_usd: float | None) -> None: ...
    def close(self) -> None: ...


class NullTranscript:
    """Does nothing — used when session transcripts are disabled."""

    path: Path | None = None

    def user(self, text: str, confidence: float) -> None: ...
    def assistant(self, text: str) -> None: ...
    def tool(self, name: str, arguments: dict[str, Any], ok: bool, detail: str) -> None: ...
    def note(self, text: str) -> None: ...
    def record_usage(self, in_tokens: int, out_tokens: int, cost_usd: float | None) -> None: ...
    def close(self) -> None: ...


class FileTranscript:
    """Appends a readable Markdown transcript for one session."""

    def __init__(self, directory: str | Path, header: str = "") -> None:
        started = datetime.now()
        self.path = Path(directory).expanduser() / f"session-{started:%Y%m%d-%H%M%S}.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open = self.path.open("a", encoding="utf-8")
        # Running cloud-usage tally for the session footer (cloud only — local
        # turns never call record_usage, so a local session shows no cost block).
        self._usage_turns = 0
        self._total_in = 0
        self._total_out = 0
        self._total_cost = 0.0
        self._has_cost = False
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

    def record_usage(self, in_tokens: int, out_tokens: int, cost_usd: float | None) -> None:
        """Record one cloud turn's token usage (and cost) for the session summary."""
        self._usage_turns += 1
        self._total_in += in_tokens
        self._total_out += out_tokens
        cost_str = ""
        if cost_usd is not None:
            self._total_cost += cost_usd
            self._has_cost = True
            cost_str = f" · ~${cost_usd:.5f}"
        self._write(
            f"> _{self._time()} · usage: context {in_tokens:,} tok, "
            f"output {out_tokens:,} tok{cost_str}_\n\n"
        )

    def _usage_footer(self) -> str:
        """The session totals block (empty for local-only sessions)."""
        if not self._usage_turns:
            return ""
        total = self._total_in + self._total_out
        cost = f" · est. cost ~${self._total_cost:.4f}" if self._has_cost else ""
        return (
            "\n---\n\n"
            f"**Cloud usage this session** — {self._usage_turns} request(s) · "
            f"context {self._total_in:,} tok · output {self._total_out:,} tok · "
            f"total {total:,} tok{cost}\n"
        )

    def close(self) -> None:
        """Write the usage summary + footer and close the file."""
        self._write(self._usage_footer())
        self._write(f"\n_session ended {datetime.now():%Y-%m-%d %H:%M:%S}_\n")
        self._open.close()
