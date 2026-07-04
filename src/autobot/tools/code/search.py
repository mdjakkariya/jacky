"""File navigation tools for the coder profile: glob + grep (path-jailed, pure Python).

``glob`` lists files matching a shell-style pattern under a jailed root, newest first;
``grep`` searches file contents with a regular expression. Both walk the tree with
``pathlib``/``os`` (no external binary, so behaviour is identical on every OS) and cap
their results so a large tree can't flood the model. Every root is resolved through the
shared :class:`~autobot.tools.access.AccessBroker`.
"""

from __future__ import annotations

import os
import re
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


_GREP_LIMIT = 200  # max result paths/lines/counts returned
_GREP_MAX_FILE_BYTES = 1_000_000  # skip files larger than this in the walk
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache", ".tox"}
)


def _iter_files(base: Path, glob_filter: str | None) -> list[Path]:
    """Files under ``base`` (noise dirs pruned, huge files skipped), optionally glob-filtered."""
    out: list[Path] = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            fp = Path(root) / name
            if glob_filter and not fp.match(glob_filter):
                continue
            try:
                if fp.stat().st_size > _GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(fp)
    return out


def grep(
    pattern: str,
    broker: AccessBroker,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "files_with_matches",
) -> str:
    """Search file contents under ``path`` for a regex ``pattern`` (gated, bounded).

    ``output_mode`` is ``"files_with_matches"`` (default — one path per matching file),
    ``"content"`` (``path:line:text`` per matching line), or ``"count"`` (``path:N``).
    ``glob`` filters which files are searched (e.g. ``"*.py"``); ``ignore_case`` is
    case-insensitive matching.
    """
    if not pattern:
        return "What should I search for? Give a regex or literal text."
    if output_mode not in ("files_with_matches", "content", "count"):
        return "output_mode must be 'files_with_matches', 'content', or 'count'."
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"That search pattern isn't valid: {exc}"
    try:
        base = broker.ensure(path or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to search."

    results: list[str] = []
    truncated = False
    for fp in _iter_files(base, glob):
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:  # binary
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        if output_mode == "files_with_matches":
            results.append(str(fp))
        elif output_mode == "count":
            results.append(f"{fp}:{len(hits)}")
        else:  # content
            results.extend(f"{fp}:{lineno}:{ln}" for lineno, ln in hits)
        if len(results) >= _GREP_LIMIT:
            truncated = True
            del results[_GREP_LIMIT:]
            break

    if not results:
        return f"No matches for {pattern!r}."
    text = "\n".join(results)
    if len(text) > _OUTPUT_CHAR_CAP:
        text = text[:_OUTPUT_CHAR_CAP] + "\n…(truncated)"
        truncated = True
    tail = "\n…(results truncated; narrow the search or add a glob filter)" if truncated else ""
    _log.info("grep pattern=%r mode=%s results=%d", pattern, output_mode, len(results))
    return f"matches for {pattern!r} ({output_mode}):\n{text}{tail}"
