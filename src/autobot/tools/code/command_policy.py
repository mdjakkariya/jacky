"""Command allow/blocklist classification for shell commands the coding agent proposes.

``classify_command`` decides whether a shell command should be blocked outright, run
without confirmation, or held for approval — before it ever reaches an executor. A
built-in read-only baseline (:func:`is_read_only_command`) lets genuinely read-only
commands (test/build/lint runners, ``git status``/``diff``, ``ls``/``cat``/``grep``,
``--version``/``--help``) run without a prompt, symmetric with the dangerous baseline. The
built-in dangerous-command baseline is matched with anchored regular expressions
precise enough to catch genuinely catastrophic invocations (whole-filesystem wipes,
fork bombs, piping a download straight into a shell, formatting/overwriting a raw
disk) without flagging everyday subdirectory operations like ``rm -rf build`` or
``chmod -R 777 ./dist``. User-supplied allow/blocklists are matched more loosely (glob
or substring) since those reflect explicit user intent rather than a hard safety
backstop. This module has no knowledge of the filesystem, the shell, or a running
process, and it never raises: unrecognized input falls back to the safe "needs
approval" decision.
"""

from __future__ import annotations

import fnmatch
import re

#: Decision returned by classify_command: outright refused, run freely, or ask first.
Decision = str

# Built-in baseline of unambiguously destructive commands, as anchored, case-insensitive
# regular expressions matched against the whitespace-normalized command via `.search`.
# Kept intentionally small and precise — this is a last-resort backstop, not the primary
# defense (that's the permission gate) — and deliberately does NOT match subdirectory
# operations (e.g. `rm -rf build`, `chmod -R 777 ./dist`) that a coding agent runs
# constantly.
_DANGEROUS: tuple[re.Pattern[str], ...] = (
    # Whole-filesystem/home wipes: `rm -rf`/`rm -fr` (any flag order, optionally with
    # `--no-preserve-root`) targeting exactly `/`, `/*`, `~`, or `$HOME` — not a subdir.
    re.compile(
        r"\brm\s+(-\S+\s+)*-?[rf]{1,2}\S*\s+(--no-preserve-root\s+)*"
        r"(/|/\*|~|\$HOME)(\s|$|;|&|\|)",
        re.IGNORECASE,
    ),
    # Same wipe expressed with GNU long-form flags: `rm --recursive [--force] /`.
    re.compile(
        r"\brm\s+(\S+\s+)*--recursive(\s+\S+)*\s+(/|/\*|~|\$HOME)(\s|$|;|&|\|)",
        re.IGNORECASE,
    ),
    # Fork bomb, tolerant of internal spacing.
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
    # Download piped straight into a shell interpreter (curl or wget; sh/bash/zsh/etc,
    # optionally via sudo).
    re.compile(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?\w*sh\b", re.IGNORECASE),
    # Filesystem format — `mkfs` as a command, not the word inside prose.
    re.compile(r"(^|[;&|]\s*)mkfs\b", re.IGNORECASE),
    # Disk-destroying dd: writing to a raw device.
    re.compile(r"\bdd\b[^;|]*\bof=/dev/", re.IGNORECASE),
    # chmod 777 on the filesystem root only, not a subdirectory.
    re.compile(r"\bchmod\s+-R?\s*777\s+/(\s|$|;|&)", re.IGNORECASE),
    # Overwriting a raw disk device via shell redirection.
    re.compile(r">\s*/dev/(sd|nvme|disk)", re.IGNORECASE),
)

# Leading programs whose invocations only read/inspect, or run the project's own
# test/build/lint scripts (which the user has classified as safe). Matched against the
# leading program of each pipeline stage. Kept precise: only subcommands that are
# read-only for ALL argument forms are listed — e.g. `git status`/`git diff` are in, but
# `git branch`/`git remote` (which have mutating `-D`/`add` variants) and `find` (which
# has `-delete`/`-exec`) are deliberately left out so they still confirm.
_SAFE_LEADERS: tuple[str, ...] = (
    # test / build / lint runners
    r"npx\s+playwright\s+test",
    r"playwright\s+test",
    r"jest",
    r"vitest",
    r"pytest",
    r"go\s+test",
    r"cargo\s+test",
    r"(npm|pnpm|yarn)\s+(run\s+)?(test|build|lint|typecheck|check)",
    # git — read-only subcommands only
    r"git\s+(status|log|diff|show|rev-parse|ls-files)",
    r"git\s+config\s+--get",
    # filesystem / text reads
    r"ls",
    r"cat",
    r"head",
    r"tail",
    r"grep",
    r"rg",
    r"wc",
    r"pwd",
    r"stat",
    r"file",
    r"which",
    r"tree",
    r"du",
    r"df",
    r"echo",
    # package info
    r"npm\s+ls",
    r"(pip|pip3)\s+(list|show)",
)

# Safe programs a pipeline may pipe INTO — pagers/filters that only read their stdin
# (deliberately excludes `tee`/`sed -i`/`awk`, which can write files).
_SAFE_FILTERS = r"(tail|head|cat|grep|rg|less|wc|sort|uniq)"

# One safe stage: a safe leading program + args that introduce no new command or file
# write (no `|;&<>$` or backtick), optionally ending in `2>&1`.
_SAFE_STAGE = (
    r"(?:" + "|".join(_SAFE_LEADERS) + r")"
    r"(?:\s+[^|;&<>`$]*)?"
    r"(?:\s+2>&1)?"
)

# A read-only command: one safe stage, then zero or more pipes into safe filters.
_SAFE_READONLY = re.compile(
    r"^" + _SAFE_STAGE + r"(?:\s*\|\s*" + _SAFE_FILTERS + r"(?:\s+[^|;&<>`$]*)?)*\s*$",
    re.IGNORECASE,
)

# Any program invoked purely for information (`--version`/`--help`/`--list`) is read-only
# regardless of the program, provided the line has no shell operators (the char classes
# admit only plain word args).
_INFO_ONLY = re.compile(
    r"^[\w./@+-]+(?:\s+[\w./@=:+-]+)*\s+--(?:version|help|list)(?:\s+[\w./@=:+-]+)*$",
    re.IGNORECASE,
)

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize(command: str) -> str:
    """Strip and collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RUN.sub(" ", command.strip())


def _matches_dangerous_baseline(normalized: str) -> bool:
    """Return True if ``normalized`` matches any built-in dangerous-command regex."""
    return any(pattern.search(normalized) for pattern in _DANGEROUS)


def is_read_only_command(command: str) -> bool:
    """Whether ``command`` only reads/inspects (or runs a project test/build/lint script).

    A read-only command auto-runs without a confirmation prompt (still audited by the
    gate). Conservative: any file-write redirect (``>``/``>>``), command chaining
    (``;``/``&&``/``||``/``&``), command substitution (``$(...)``/backticks), or an
    unrecognized leading program makes it non-read-only, so it falls through to confirm.
    Never raises; unparseable input is treated as not read-only.
    """
    try:
        normalized = _normalize(command or "")
    except (TypeError, AttributeError):
        return False
    if not normalized or _matches_dangerous_baseline(normalized):
        return False
    return bool(_SAFE_READONLY.match(normalized) or _INFO_ONLY.match(normalized))


def _matches(pattern: str, normalized: str) -> bool:
    """Return True if ``pattern`` matches ``normalized`` by glob or substring."""
    if fnmatch.fnmatch(normalized, pattern):
        return True
    return pattern.lower() in normalized.lower()


def _matches_any(patterns: list[str], normalized: str) -> bool:
    return any(_matches(pattern, normalized) for pattern in patterns)


def classify_command(
    command: str,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> tuple[Decision, str]:
    """Classify ``command`` as ``"block"``, ``"allow"``, or ``"confirm"``.

    Precedence:
        1. The built-in dangerous baseline or the user ``blocklist`` -> ``"block"``.
        2. Else the user ``allowlist`` -> ``"allow"``.
        3. Else -> ``"confirm"`` (needs approval).

    A pattern matches the normalized command (stripped, internal whitespace collapsed)
    if it is a glob match (``fnmatch``, so ``"git *"`` matches ``"git status"``) or a
    case-insensitive substring of it (so ``"rm -rf /"`` matches inside a longer
    command). Never raises; ``None`` lists are treated as empty, and unmatched or empty
    input safely falls back to ``"confirm"``.

    Args:
        command: The raw shell command text to classify.
        allowlist: Patterns the user has pre-approved to run without confirmation.
        blocklist: Patterns the user has pre-refused, always blocked.

    Returns:
        A tuple of ``(decision, reason)``.
    """
    try:
        normalized = _normalize(command or "")
    except (TypeError, AttributeError):
        return "confirm", "not on the allowlist; needs approval"

    if not normalized:
        return "confirm", "not on the allowlist; needs approval"

    block_patterns = list(blocklist) if blocklist else []
    allow_patterns = list(allowlist) if allowlist else []

    if _matches_dangerous_baseline(normalized):
        return "block", "matches a built-in dangerous command pattern"
    if _matches_any(block_patterns, normalized):
        return "block", "matches a user blocklist entry"

    if _matches_any(allow_patterns, normalized):
        return "allow", "matches a user allowlist entry"

    if is_read_only_command(normalized):
        return "allow", "safe read-only command"

    return "confirm", "not on the allowlist; needs approval"
