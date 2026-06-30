"""Find and open files on the Mac via Spotlight (``mdfind``) — on-device.

Two read/act tools:

* ``search_files`` — locate files by name. The query is split into words and turned
  into a Spotlight *filename* predicate where every word must appear (case- and
  diacritic-insensitive), so word order, underscores-vs-spaces and partial words all
  match (``"certificate internship"`` finds ``certificate_internship.pdf``). When
  nothing matches exactly, it relaxes progressively so a misheard or mistyped name
  still surfaces close files: first it retries with each word *prefix-relaxed* (the
  start kept, a wrong ending dropped — ``"Autobot"`` still finds ``AutoBoard``),
  keeping the match narrow; only then does it widen to *any* word. Results are ranked
  (whole-word matches first, then prefix-only, then most-recently-modified) and
  returned with full paths.
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


# Below this length a token is too short to relax safely (a 3-char prefix matches
# huge swaths of the disk), so short tokens are matched whole.
_MIN_PREFIX_LEN = 4


def _prefix(token: str) -> str:
    """A relaxed prefix of a token, to tolerate a misheard or mistyped word *ending*.

    Speech-to-text most often gets the *tail* of a name wrong ('Autobot' heard as
    'AutoBoard', 'Samastra' as 'Samastor'), while the start is usually right. Dropping
    up to the last two characters — but never below :data:`_MIN_PREFIX_LEN` — lets the
    start still match the real file. Short tokens are returned unchanged.
    """
    if len(token) <= _MIN_PREFIX_LEN:
        return token
    return token[: max(_MIN_PREFIX_LEN, len(token) - 2)]


def _name_predicate(tokens: list[str], joiner: str) -> str:
    """Spotlight predicate matching the file *name* against each token.

    ``"*tok*"cd`` is a case- and diacritic-insensitive substring match, joined by
    ``&&`` (all words) or ``||`` (any word). The ``cd`` flags are why casing and
    accents never matter: 'certificate' matches ``CERTIficate`` and ``cért``.
    """
    parts = [f'kMDItemFSName == "*{t}*"cd' for t in tokens]
    return f" {joiner} ".join(parts)


# A spoken file "type" (a word a user naturally says) → the Spotlight content-type
# UTIs that name it. ``kMDItemContentTypeTree`` carries a file's whole type hierarchy,
# so one broad UTI (``public.image``) matches every concrete image format (png, jpeg,
# heic, …) without us enumerating extensions. Several words can map to one category.
_DOC_UTIS = (
    "public.text",
    "com.microsoft.word.doc",
    "org.openxmlformats.wordprocessingml.document",
    "com.apple.iwork.pages.pages",
    "com.apple.rtfd",
    "public.rtf",
)
_TYPE_UTIS: dict[str, tuple[str, ...]] = {
    "pdf": ("com.adobe.pdf",),
    "image": ("public.image",),
    "photo": ("public.image",),
    "picture": ("public.image",),
    "audio": ("public.audio",),
    "music": ("public.audio",),
    "song": ("public.audio",),
    "video": ("public.movie",),
    "movie": ("public.movie",),
    "spreadsheet": ("public.spreadsheet",),
    "excel": ("public.spreadsheet",),
    "presentation": ("public.presentation",),
    "slides": ("public.presentation",),
    "keynote": ("public.presentation",),
    "powerpoint": ("public.presentation",),
    "document": _DOC_UTIS,
    "doc": _DOC_UTIS,
    "word": _DOC_UTIS,
    "text": ("public.text",),
}


def _type_predicate(file_type: str | None) -> str | None:
    """Spotlight predicate restricting to a file *type*, or ``None`` to not restrict.

    A known category word (``"pdf"``, ``"photo"``, ``"spreadsheet"`` …) maps to its
    content-type UTIs. An unrecognised but extension-shaped word (``"xlsx"``,
    ``"heic"``) falls back to a filename-extension match, so 'the xlsx one' still
    narrows. Anything else is ignored (returns ``None``) rather than over-filtering.
    """
    if not file_type:
        return None
    key = file_type.strip().lower().lstrip(".")
    if not key:
        return None
    utis = _TYPE_UTIS.get(key)
    if utis:
        return " || ".join(f'kMDItemContentTypeTree == "{u}"' for u in utis)
    if key.isalnum() and len(key) <= 5:  # looks like a bare extension, e.g. "xlsx"
        return f'kMDItemFSName == "*.{key}"c'
    return None


def _kinds_present(paths: list[str]) -> list[str]:
    """Distinct file extensions among ``paths``, in first-seen order (e.g. ``.pdf``).

    Used to tell the user when results span several formats, so the assistant can
    offer to narrow by type ('these are .pdf, .xlsx and .md — which did you mean?').
    """
    kinds: list[str] = []
    for p in paths:
        ext = Path(p).suffix.lower()
        if ext and ext not in kinds:
            kinds.append(ext)
    return kinds


def _kinds_phrase(kinds: list[str]) -> str:
    """Join extensions for prose: ``['.pdf', '.md']`` → ``'.pdf and .md'``."""
    if len(kinds) <= 1:
        return kinds[0] if kinds else "files"
    return ", ".join(kinds[:-1]) + f" and {kinds[-1]}"


def _tilde(path: str) -> str:
    """Shorten a home-relative path to ``~/…`` for readable output."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


# Characters that begin a new "word" inside a filename. A token that lands right
# after one of these (or at the very start) is a word-boundary match, which reads as
# a much stronger hit than the same letters buried mid-word.
_WORD_BOUNDARY = frozenset(" -_.,/()[]")


def _token_score(name: str, token: str, prefix: str) -> int:
    """How strongly one query word matches a filename: whole-word and boundary win.

    Mirrors what fuzzy finders reward: a whole-word hit beats a prefix-only (fuzzy)
    hit, and a hit at a word boundary beats one buried mid-word. Scores: 3 = whole
    word at a boundary, 2 = whole word anywhere, 1 = prefix-only, 0 = no match.
    """
    idx = name.find(token)
    if idx != -1:
        return 3 if (idx == 0 or name[idx - 1] in _WORD_BOUNDARY) else 2
    if prefix and prefix != token:
        pidx = name.find(prefix)
        if pidx != -1:
            return 1
    return 0


def _rank(paths: list[str], tokens: list[str], mtime_of: MtimeFn = _safe_mtime) -> list[str]:
    """Order hits by match strength, then shorter name, then most recent, then name.

    Strength sums :func:`_token_score` over the query words, so a file matching more
    words — each as a whole word, ideally at a word boundary — ranks above one with
    weaker, scattered, or prefix-only matches. Ties prefer the shorter name (less
    extra noise around the match, à la fzf) and then the most recently modified file.
    """
    toks = [t.lower() for t in tokens]
    prefixes = [_prefix(t) for t in toks]
    unique = list(dict.fromkeys(p for p in paths if p))  # dedupe, keep first order

    def key(p: str) -> tuple[int, int, float, str]:
        name = Path(p).name.lower()
        score = sum(_token_score(name, t, pre) for t, pre in zip(toks, prefixes, strict=True))
        return (-score, len(name), -mtime_of(p), name)

    return sorted(unique, key=key)


def format_results(
    query: str,
    paths: list[str],
    total: int | None = None,
    fuzzy: bool = False,
    file_type: str | None = None,
    type_missed: bool = False,
    kinds: list[str] | None = None,
) -> str:
    """Render ranked hits into a numbered, open-friendly summary.

    ``file_type`` is the type the user asked for (if any); ``type_missed`` is set when
    that type matched nothing so we fell back to other formats. ``kinds`` is the set of
    extensions across the *whole* result set (not just the shown rows) — when a result
    is too broad and spans several, the summary names them so the assistant can offer
    to narrow by type instead of just asking for "another word". Defaults to the kinds
    of the shown ``paths`` when not supplied.
    """
    if not paths:
        of_type = f" of type {file_type}" if file_type else ""
        return (
            f"I couldn't find any files matching '{query}'{of_type}. "
            "Try fewer words, or different words from the file name."
        )
    total = total if total is not None else len(paths)
    lines = [f"{i}. {Path(p).name}\n   {_tilde(p)}" for i, p in enumerate(paths, 1)]
    kinds = kinds if kinds is not None else _kinds_present(paths)
    if type_missed:
        head = (
            f"I didn't find any {file_type} files for '{query}', but here are the closest "
            f"matches ({_kinds_phrase(kinds)})"
        )
    elif total > _TOO_MANY:
        head = (
            f"That matched {total} files — too broad to be sure which you mean. Here are "
            f"the {len(paths)} most relevant"
        )
        if len(kinds) > 1:
            head += (
                f". They're a mix of {_kinds_phrase(kinds)} — tell me the type "
                "(PDF, document, image…) or a more specific word from the name"
            )
        else:
            head += "; if none fit, tell me a more specific word from the name"
    elif fuzzy:
        head = f"No exact match for '{query}', but here are the closest files"
    else:
        ftype = f" {file_type}" if file_type else ""
        head = f"Found {total}{ftype} file{'s' if total != 1 else ''} matching '{query}'"
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
    file_type: str | None = None,
    runner: Runner | None = None,
    mtime_of: MtimeFn = _safe_mtime,
    choices: ChoicesSink | None = None,
) -> str:
    """Search the user's home folder for files whose name matches ``query``.

    ``file_type`` (optional) restricts to a kind — 'pdf', 'document', 'image',
    'audio', 'video', 'spreadsheet', or a bare extension like 'xlsx' — using
    Spotlight's content-type index. If that type matches nothing, the search retries
    without it and flags that no file of that type was found.

    When a ``choices`` sink is wired, the ranked results are also published as a
    clickable card (Open / Reveal / Copy path); in chat it renders in the drawer, in
    voice it's a transient preview on the orb. The returned text is unchanged, so
    voice still works off the spoken reply.
    """
    run = runner or _subprocess_runner
    toks = _tokens(query or "")
    if not toks:
        return "Tell me what to look for — a word or two from the file name."
    home = str(Path.home())
    prefixes = [_prefix(t) for t in toks]
    relaxable = prefixes != toks  # at least one token is long enough to relax
    type_pred = _type_predicate(file_type)

    def find(
        search_tokens: list[str], joiner: str, with_type: bool
    ) -> tuple[list[str] | None, str]:
        name_pred = _name_predicate(search_tokens, joiner)
        pred = f"({name_pred}) && ({type_pred})" if (with_type and type_pred) else name_pred
        rc, out = run(["mdfind", "-onlyin", home, pred])
        if rc != 0:
            return None, out
        return [p for p in (s.strip() for s in out.splitlines()) if p], out

    # Progressive fallback when nothing matches exactly. Each tier widens the net just
    # enough, so a misheard or mistyped name still surfaces close files without
    # drowning the user in matches of one common word:
    #   0. every word, exact (as spoken)
    #   1. every word, prefix-relaxed (tolerate a wrong ending; stays narrow)
    #   2. any word, exact (a multi-word query where only some words are right)
    #   3. any word, prefix-relaxed (last resort)
    def search(with_type: bool) -> tuple[list[str] | None, bool, str]:
        paths, out = find(toks, "&&", with_type)
        if paths is None:
            return None, False, out
        fuzzy = False
        tiers: list[tuple[list[str], str]] = []
        if relaxable:
            tiers.append((prefixes, "&&"))
        if len(toks) > 1:
            tiers.append((toks, "||"))
        if len(toks) > 1 and relaxable:
            tiers.append((prefixes, "||"))
        for tier_tokens, joiner in tiers:
            if paths:
                break
            alt, _ = find(tier_tokens, joiner, with_type)
            if alt:
                paths, fuzzy = alt, True
        return paths, fuzzy, out

    paths, fuzzy, out = search(with_type=bool(type_pred))
    if paths is None:
        _log.warning("search_files mdfind failed out=%r", out)
        return f"I couldn't run the search: {out.strip() or 'unknown error'}"
    # The type filter removed everything: retry without it so we can tell the user the
    # name exists but not in that format, rather than a bare "nothing found".
    type_missed = False
    if not paths and type_pred:
        alt, alt_fuzzy, _ = search(with_type=False)
        if alt:
            paths, fuzzy, type_missed = alt, alt_fuzzy, True
    total = len(paths)
    candidates = _rank(paths[:_MAX_CANDIDATES], toks, mtime_of)  # cap stat() work
    ranked = candidates[:limit]
    kinds = _kinds_present(candidates)  # across all candidates, so "mix of" is truthful
    _log.info(
        "search_files query=%r tokens=%d type=%s hits=%d fuzzy=%s",
        query,
        len(toks),
        file_type or "-",
        total,
        fuzzy,
    )
    if choices is not None and ranked:
        choices(f"Files matching '{query}'", _choice_items(ranked))
    return format_results(
        query,
        ranked,
        total=total,
        fuzzy=fuzzy,
        file_type=file_type,
        type_missed=type_missed,
        kinds=kinds,
    )


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
                "order and casing don't matter and partial words work, so prefer the user's "
                "own words over guessing an exact filename. If the user names a kind of file "
                "('the PDF', 'a photo', 'the spreadsheet'), pass it as `file_type` to narrow "
                "the search. If the result says the matches are a mix of types (e.g. .pdf, "
                ".docx, .xlsx) or that it's too broad, ASK the user which type they mean "
                "(PDF, document, image, audio, video, spreadsheet) and search again with "
                "`file_type` set. If results come back as 'closest files', they're fuzzy "
                "suggestions: show them and let the user pick. To open one of the results, "
                "use open_path with its full path — do NOT use open_website for local files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words from the file name to search for.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": (
                            "Optional. Restrict to a kind of file when the user names one: "
                            "'pdf', 'document', 'image', 'audio', 'video', 'spreadsheet', "
                            "'presentation', or a bare extension like 'xlsx'. Omit if unsure."
                        ),
                    },
                },
                "required": ["query"],
            },
            handler=lambda query, file_type=None: search_files(
                query, file_type=file_type, runner=runner, choices=choices
            ),
            risk=Risk.READ_ONLY,
            ack="Searching your files.",
            core=True,
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
