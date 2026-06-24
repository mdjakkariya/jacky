"""Find and open files on the Mac via Spotlight (``mdfind``) — on-device.

Two read/act tools:

* ``search_files`` — locate files by name. The query is split into words and turned
  into a Spotlight *filename* predicate where every word must appear (case- and
  diacritic-insensitive), so word order, underscores-vs-spaces and partial words all
  match (``"certificate internship"`` finds ``certificate_internship.pdf``). When
  nothing matches every word, it falls back to *any* word so the user still gets
  close suggestions instead of an empty result. Results are ranked (most words
  matched first, then most-recently-modified) and returned with full paths.
* ``open_path`` — open a file or folder that search found, with its default app.

Spotlight is already indexed locally, so this stays on-device: the query never
leaves the machine, and search is scoped to the user's home folder. The shell call
goes through an injectable runner and the formatting/ranking are pure functions, so
everything is unit-tested without touching Spotlight or the filesystem.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from autobot.core.events import ChoicesSink
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("tools")

# (argv) -> (returncode, output). Injectable so tests don't run mdfind/open.
Runner = Callable[[list[str]], tuple[int, str]]
# (path) -> modification time. Injectable so ranking is testable off-disk.
MtimeFn = Callable[[str], float]

_DEFAULT_LIMIT = 8
# Above this, a result set is too broad to be useful — we ask the user to narrow
# rather than listing the recency-sorted top of thousands of weak matches.
_TOO_MANY = 60
# Only rank/stat this many hits: ranking stats every candidate for its mtime, so an
# unbounded match (a common word -> thousands of hits) would stat thousands of files
# and stall the turn. A broad match is flagged "too broad" anyway, so capping here
# costs nothing useful.
_MAX_CANDIDATES = 200

# Filler/meta words a user (or the model) tends to wrap a name in — 'find the file
# with the name X'. Searching for these matches huge swaths of the disk ('name' is
# a substring of filename/username/rename…), so we strip them and keep the
# distinctive words. Kept deliberately small and obviously-non-distinctive.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "your",
        "this",
        "that",
        "file",
        "files",
        "folder",
        "document",
        "documents",
        "doc",
        "docs",
        "name",
        "named",
        "called",
        "titled",
        "find",
        "search",
        "look",
        "locate",
        "open",
        "show",
        "get",
        "for",
        "of",
        "with",
        "in",
        "on",
        "me",
        "please",
        "where",
        "is",
    }
)


def _subprocess_runner(argv: list[str]) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(argv, capture_output=True, text=True)
    out = (proc.stdout or "") if proc.returncode == 0 else (proc.stderr or proc.stdout or "")
    return proc.returncode, out


def _safe_mtime(path: str) -> float:
    """Modification time, or 0.0 if the file is gone (never raises)."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _tokens(query: str) -> list[str]:
    """Split a query into meaningful search words.

    Quotes/slashes are dropped (they'd break the predicate) and common filler words
    ('file', 'name', 'the', 'find', …) are removed, so 'a file with the name
    certificate' searches for just 'certificate' instead of matching every file that
    contains the word 'name'. If the query is *all* filler, the raw words are kept.
    """
    cleaned = query.replace('"', " ").replace("/", " ")
    raw = [t for t in cleaned.split() if t]
    meaningful = [t for t in raw if t.lower() not in _STOPWORDS]
    return meaningful or raw


def _name_predicate(tokens: list[str], joiner: str) -> str:
    """Spotlight predicate matching the file *name* against each token.

    ``"*tok*"cd`` is a case- and diacritic-insensitive substring match, joined by
    ``&&`` (all words) or ``||`` (any word).
    """
    parts = [f'kMDItemFSName == "*{t}*"cd' for t in tokens]
    return f" {joiner} ".join(parts)


def _tilde(path: str) -> str:
    """Shorten a home-relative path to ``~/…`` for readable output."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def _rank(paths: list[str], tokens: list[str], mtime_of: MtimeFn = _safe_mtime) -> list[str]:
    """Order hits by words-matched-in-name (desc), then most recent, then name."""
    toks = [t.lower() for t in tokens]
    unique = list(dict.fromkeys(p for p in paths if p))  # dedupe, keep first order

    def key(p: str) -> tuple[int, float, str]:
        name = Path(p).name.lower()
        matched = sum(1 for t in toks if t in name)
        return (-matched, -mtime_of(p), name)

    return sorted(unique, key=key)


def format_results(
    query: str, paths: list[str], total: int | None = None, fuzzy: bool = False
) -> str:
    """Render ranked hits into a numbered, open-friendly summary."""
    if not paths:
        return (
            f"I couldn't find any files matching '{query}'. "
            "Try fewer words, or different words from the file name."
        )
    total = total if total is not None else len(paths)
    lines = [f"{i}. {Path(p).name}\n   {_tilde(p)}" for i, p in enumerate(paths, 1)]
    if total > _TOO_MANY:
        head = (
            f"That matched {total} files — too broad to be sure which you mean. Here are "
            f"the {len(paths)} most recent; if none fit, tell me a more specific word "
            "from the name"
        )
    elif fuzzy:
        head = f"No exact match for '{query}', but here are the closest files"
    else:
        head = f"Found {total} file{'s' if total != 1 else ''} matching '{query}'"
        if total > len(paths):
            head += f" (showing the top {len(paths)})"
    foot = "\nTell me which one to open (by number or name) and I'll open it."
    return head + ":\n" + "\n".join(lines) + foot


def _choice_items(paths: list[str]) -> list[dict[str, Any]]:
    """Build generic choice items (label + Open/Reveal/Copy actions) for the chat card.

    Each item maps to one file: 'Open' and 'Reveal' run tools through the gate;
    'Copy path' is handled client-side. The path is carried in the action args.
    """
    items: list[dict[str, Any]] = []
    for p in paths:
        items.append(
            {
                "label": Path(p).name,
                "sublabel": _tilde(p),
                "actions": [
                    {"label": "Open", "tool": "open_path", "args": {"path": p}},
                    {"label": "Reveal", "tool": "reveal_path", "args": {"path": p}},
                    {"label": "Copy path", "copy": p},
                ],
            }
        )
    return items


def search_files(
    query: str,
    limit: int = _DEFAULT_LIMIT,
    runner: Runner | None = None,
    mtime_of: MtimeFn = _safe_mtime,
    choices: ChoicesSink | None = None,
) -> str:
    """Search the user's home folder for files whose name matches ``query``.

    When a ``choices`` sink is wired, the ranked results are also published as a
    clickable card (Open / Reveal / Copy path) for the chat drawer; the returned
    text is unchanged, so voice still works off the spoken reply.
    """
    run = runner or _subprocess_runner
    toks = _tokens(query or "")
    if not toks:
        return "Tell me what to look for — a word or two from the file name."
    home = str(Path.home())

    def find(joiner: str) -> tuple[list[str] | None, str]:
        rc, out = run(["mdfind", "-onlyin", home, _name_predicate(toks, joiner)])
        if rc != 0:
            return None, out
        return [p for p in (s.strip() for s in out.splitlines()) if p], out

    paths, out = find("&&")
    if paths is None:
        _log.warning("search_files mdfind failed out=%r", out)
        return f"I couldn't run the search: {out.strip() or 'unknown error'}"
    fuzzy = False
    if not paths and len(toks) > 1:
        alt, _ = find("||")  # fuzzy: any word, so the user still gets suggestions
        if alt:
            paths, fuzzy = alt, True
    total = len(paths)
    ranked = _rank(paths[:_MAX_CANDIDATES], toks, mtime_of)[:limit]  # cap stat() work
    _log.info("search_files query=%r tokens=%d hits=%d fuzzy=%s", query, len(toks), total, fuzzy)
    if choices is not None and ranked:
        choices(f"Files matching '{query}'", _choice_items(ranked))
    return format_results(query, ranked, total=total, fuzzy=fuzzy)


def reveal_path(path: str, runner: Runner | None = None) -> str:
    """Reveal a file or folder in Finder (select it in a window) via ``open -R``."""
    run = runner or _subprocess_runner
    raw = (path or "").strip()
    if not raw:
        return "Tell me which file to reveal."
    expanded = Path(raw).expanduser()
    if not expanded.exists():
        return f"I can't find that file: {raw}"
    rc, out = run(["open", "-R", str(expanded)])
    if rc != 0:
        return f"I couldn't reveal it: {out.strip() or 'unknown error'}"
    _log.info("reveal_path revealed name=%r", expanded.name)
    return f"Showing {expanded.name} in Finder."


def open_path(path: str, runner: Runner | None = None) -> str:
    """Open a file or folder (by path) with its default app via ``open``."""
    run = runner or _subprocess_runner
    raw = (path or "").strip()
    if not raw:
        return "Tell me which file to open."
    expanded = Path(raw).expanduser()  # `open` doesn't expand ~ itself
    if not expanded.exists():
        return f"I can't find that file: {raw}"
    rc, out = run(["open", str(expanded)])
    if rc != 0:
        return f"I couldn't open it: {out.strip() or 'unknown error'}"
    _log.info("open_path opened name=%r", expanded.name)
    return f"Opened {expanded.name}."


def register_file_tools(
    registry: ToolRegistry,
    runner: Runner | None = None,
    choices: ChoicesSink | None = None,
) -> None:
    """Register the file tools: ``search_files``, ``open_path``, ``reveal_path``.

    When ``choices`` is wired, search results are also surfaced as a clickable card
    (Open / Reveal / Copy path) in the chat drawer.
    """
    registry.register(
        ToolSpec(
            name="search_files",
            description=(
                "Find files on this Mac by name using Spotlight (on-device; searches the "
                "user's home folder). Use when the user wants to locate a file or document. "
                "Spoken cues: 'find my <file>', 'where is my <file>', 'search for a file "
                "called <name>', 'find the <something> document'. Pass ONLY the distinctive "
                "words from the name as `query` (e.g. 'internship certificate', 'invoice "
                "march') — leave out filler like 'file', 'document', 'name', 'called'. Word "
                "order doesn't matter and partial words work, so prefer the user's own words "
                "over guessing an exact filename. If the result says it's too broad, ask the "
                "user for a more specific word. If results come back as 'closest "
                "files', they're fuzzy suggestions: show them and let the user pick. To open "
                "one of the results, use open_path with its full path — do NOT use "
                "open_website for local files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words from the file name to search for.",
                    }
                },
                "required": ["query"],
            },
            handler=lambda query, limit=_DEFAULT_LIMIT: search_files(
                query, limit, runner, choices=choices
            ),
            risk=Risk.READ_ONLY,
            ack="Searching your files.",
        )
    )
    registry.register(
        ToolSpec(
            name="open_path",
            description=(
                "Open a local file or folder with its default app, given its path (e.g. the "
                "full path of a result from search_files). Use this — not open_website — to "
                "open files found on disk. Spoken cues after a search: 'open it', 'open the "
                "first one', 'open that PDF'. The path may start with ~ for the home folder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Full path to the file or folder to open.",
                    }
                },
                "required": ["path"],
            },
            handler=lambda path: open_path(path, runner),
            risk=Risk.WRITE,
            ack="Opening it.",
        )
    )
    registry.register(
        ToolSpec(
            name="reveal_path",
            description=(
                "Show a local file or folder in Finder (select it in a window) without "
                "opening it, given its path. Spoken cues: 'reveal it', 'show it in Finder', "
                "'where is it on disk'. The path may start with ~ for the home folder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Full path to the file or folder to reveal.",
                    }
                },
                "required": ["path"],
            },
            handler=lambda path: reveal_path(path, runner),
            risk=Risk.WRITE,
            ack="Showing it in Finder.",
        )
    )
