"""File navigation tools for the coder profile: glob, grep, list_dir (path-jailed, pure Python).

``glob`` lists files matching a shell-style pattern under a jailed root, newest first;
``grep`` searches file contents with a regular expression; ``list_dir`` lists a folder's
immediate entries. All walk the tree with ``pathlib``/``os`` (no external binary, so
behaviour is identical on every OS), prune noise dirs (``.git``/``node_modules``/…), and cap
their results so a large tree can't flood the model. Every root is resolved through the
shared :class:`~autobot.tools.access.AccessBroker`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.access import AccessBroker, AccessDeniedError
from autobot.tools.registry import ToolRegistry, ToolSpec

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
    # Prune noise dirs (.git/node_modules/…) so a broad '**/*' isn't flooded with junk that
    # exhausts the result cap — matching grep/repo_map, which already skip these.
    matches = [p for p in matches if not (_SKIP_DIRS & set(p.relative_to(base).parts))]
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
_GREP_TIMEOUT_S = 20.0  # cap the ripgrep subprocess so a pathological tree can't hang the turn
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache", ".tox"}
)


def _bound_grep_output(pattern: str, output_mode: str, raw: str) -> str:
    """Format raw match lines into the standard grep result, capped by line and char budget."""
    if not raw.strip():
        return f"No matches for {pattern!r}."
    lines = raw.splitlines()
    truncated = len(lines) > _GREP_LIMIT
    text = "\n".join(lines[:_GREP_LIMIT])
    if len(text) > _OUTPUT_CHAR_CAP:
        text = text[:_OUTPUT_CHAR_CAP] + "\n…(truncated)"
        truncated = True
    tail = "\n…(results truncated; narrow the search or add a glob filter)" if truncated else ""
    return f"matches for {pattern!r} ({output_mode}):\n{text}{tail}"


def _ripgrep(
    pattern: str, base: Path, glob_filter: str | None, ignore_case: bool, mode: str, context: int
) -> str | None:
    """Search via ripgrep when installed, else return ``None`` to fall back to the Python scan.

    Falls back (returns ``None``) if rg is missing, times out, or errors. Paths are absolute (rg
    is given the absolute ``base``), matching the fallback so ``read_file`` can open them directly.
    """
    if shutil.which("rg") is None:
        return None
    args = ["rg", "--color", "never", "--no-messages"]
    if ignore_case:
        args.append("--ignore-case")
    if glob_filter:
        args += ["--glob", glob_filter]
    if mode == "files_with_matches":
        args.append("--files-with-matches")
    elif mode == "count":
        args.append("--count-matches")
    else:  # content
        args.append("--line-number")
        if context > 0:
            args += ["--context", str(context)]
    args += ["--", pattern, str(base)]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_GREP_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # 0 = matches, 1 = no matches; anything else → fall back
        return None
    _log.info("grep(rg) pattern=%r mode=%s", pattern, mode)
    return _bound_grep_output(pattern, mode, proc.stdout)


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
    context: int = 0,
) -> str:
    """Search file contents under ``path`` for a regex ``pattern`` (gated, bounded).

    Uses ripgrep when it's installed (fast, honors ``.gitignore``); otherwise falls back to a
    pure-Python walk that prunes noise dirs. ``output_mode`` is ``"files_with_matches"``
    (default — one path per matching file), ``"content"`` (``path:line:text`` per matching
    line), or ``"count"`` (``path:N``). ``glob`` filters which files are searched (e.g.
    ``"*.py"``); ``ignore_case`` is case-insensitive; ``context`` adds N lines of surrounding
    context around each match (``content`` mode only).
    """
    if not pattern:
        return "What should I search for? Give a regex or literal text."
    if output_mode not in ("files_with_matches", "content", "count"):
        return "output_mode must be 'files_with_matches', 'content', or 'count'."
    context = max(0, min(context, 10))  # keep context bounded so a match can't dump a file
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

    rg = _ripgrep(pattern, base, glob, ignore_case, output_mode, context)
    if rg is not None:
        return rg

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
            for lineno, ln in hits:
                if context:  # show N lines around the match ('-' separates context from ':' hits)
                    for j in range(max(0, lineno - 1 - context), min(len(lines), lineno + context)):
                        sep = ":" if (j + 1) == lineno else "-"
                        results.append(f"{fp}:{j + 1}{sep}{lines[j]}")
                else:
                    results.append(f"{fp}:{lineno}:{ln}")
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


_LIST_LIMIT = 300  # max entries returned by list_dir


def list_dir(broker: AccessBroker, path: str = ".") -> str:
    """List a folder's immediate entries (gated): subfolders first (with a trailing ``/``).

    Noise dirs (``.git``, ``node_modules``, …) are hidden so the listing stays signal.
    """
    try:
        base = broker.ensure(path or ".", write=False)
    except (AccessDeniedError, PermissionError) as exc:
        return str(exc)
    if not base.is_dir():
        return f"'{base.name}' is not a folder to list."
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        return f"I couldn't list {base.name}: {exc}"
    dirs = sorted(
        (p.name for p in entries if p.is_dir() and p.name not in _SKIP_DIRS), key=str.lower
    )
    files = sorted((p.name for p in entries if not p.is_dir()), key=str.lower)
    lines = [f"{d}/" for d in dirs] + files
    if not lines:
        return f"{base.name}/ is empty (nothing but hidden noise dirs, if any)."
    shown = lines[:_LIST_LIMIT]
    tail = f"\n…({len(lines) - len(shown)} more)" if len(lines) > len(shown) else ""
    _log.info("list_dir path=%s entries=%d", base.name, len(lines))
    plural = "y" if len(lines) == 1 else "ies"
    return f"{base.name}/ ({len(lines)} entr{plural}):\n" + "\n".join(shown) + tail


def register_nav_tools(registry: ToolRegistry, broker: AccessBroker) -> None:
    """Register the navigation tools (glob, grep, list_dir). All are read-only and gated."""
    registry.register(
        ToolSpec(
            name="list_dir",
            description=(
                "List the files and subfolders directly inside a folder (subfolders first, each "
                "shown with a trailing '/'). Use it to see what's in a directory before globbing "
                "or reading. Pass `path` (defaults to the working folder). Noise dirs like .git "
                "and node_modules are hidden."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder to list (optional)."},
                },
                "required": [],
            },
            handler=lambda path=".": list_dir(broker, path),
            risk=Risk.READ_ONLY,
            ack="Listing files.",
        )
    )
    registry.register(
        ToolSpec(
            name="glob",
            description=(
                "List files whose path matches a shell glob (e.g. '**/*.py', 'src/**/*.ts'), "
                "newest first. Use this to find files by name/location before reading them. "
                "Pass `path` to search a subfolder. For searching file CONTENTS, use grep."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
                    "path": {"type": "string", "description": "Folder to search in (optional)."},
                },
                "required": ["pattern"],
            },
            handler=lambda pattern="", path=".": glob_files(pattern, broker, path),
            risk=Risk.READ_ONLY,
            ack="Looking for files.",
        )
    )

    # A nested typed def (not a lambda) keeps the handler under the 100-char line limit
    # while staying no-arg-safe; it closes over ``broker``.
    def _grep_handler(
        pattern: str = "",
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        output_mode: str = "files_with_matches",
        context: int = 0,
    ) -> str:
        return grep(pattern, broker, path, glob, ignore_case, output_mode, context)

    registry.register(
        ToolSpec(
            name="grep",
            description=(
                "Search file contents for a regular expression (uses ripgrep when available). "
                "`output_mode`: 'files_with_matches' (default, paths only), 'content' "
                "(path:line:text), or 'count' (path:N). Filter files with `glob` (e.g. '*.py'); "
                "set `ignore_case`; set `context` (content mode) for N lines around each match. "
                "Use this to find where code/text lives."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex or literal to search for."},
                    "path": {"type": "string", "description": "Folder to search in (optional)."},
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob.",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive (default false).",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content", "count"],
                        "description": "How to report matches (default files_with_matches).",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context around each match (content mode).",
                    },
                },
                "required": ["pattern"],
            },
            handler=_grep_handler,
            risk=Risk.READ_ONLY,
            ack="Searching the code.",
        )
    )
    _log.info("nav tools registered (glob/grep/list_dir)")
