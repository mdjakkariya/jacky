"""File navigation tools for the coder profile: glob + grep (path-jailed, pure Python).

``glob`` lists files matching a shell-style pattern under a jailed root, newest first;
``grep`` searches file contents with a regular expression. Both walk the tree with
``pathlib``/``os`` (no external binary, so behaviour is identical on every OS) and cap
their results so a large tree can't flood the model. Every root is resolved through the
shared :class:`~autobot.tools.access.AccessBroker`.
"""

from __future__ import annotations

from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError

_log = get_logger("coder")

_GLOB_LIMIT = 100  # max file paths returned by glob
_OUTPUT_CHAR_CAP = 60_000  # max chars returned by glob/grep into the conversation


def _safe_mtime(p: Path) -> float:
    """The file's mtime, or 0.0 if it can't be stat'd (never raises)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def glob_files(pattern: str, broker: AccessBroker, path: str = ".") -> str:
    """List files matching a shell glob ``pattern`` under ``path`` (gated), newest first."""
    if not pattern:
        return "What should I match? Give a glob pattern like '**/*.py'."
    try:
        base = broker.ensure(path or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to search."
    try:
        matches = [p for p in base.glob(pattern) if p.is_file()]
    except (OSError, ValueError, NotImplementedError) as exc:
        return f"I couldn't search with that pattern: {exc}"
    if not matches:
        return f"No files match {pattern!r} under {base}."
    matches.sort(key=_safe_mtime, reverse=True)
    shown = matches[:_GLOB_LIMIT]
    text = "\n".join(str(p) for p in shown)
    if len(text) > _OUTPUT_CHAR_CAP:
        text = text[:_OUTPUT_CHAR_CAP] + "\n…(truncated)"
    if len(matches) > len(shown):
        tail = f"\n…({len(matches) - len(shown)} more; narrow the pattern)"
    else:
        tail = ""
    _log.info("glob pattern=%r matches=%d", pattern, len(matches))
    return f"{len(matches)} file(s) matching {pattern!r} (newest first):\n{text}{tail}"
