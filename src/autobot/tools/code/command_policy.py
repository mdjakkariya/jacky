"""Command allow/blocklist classification for shell commands the coding agent proposes.

``classify_command`` decides whether a shell command should be blocked outright, run
without confirmation, or held for approval — before it ever reaches an executor. The
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

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize(command: str) -> str:
    """Strip and collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RUN.sub(" ", command.strip())


def _matches_dangerous_baseline(normalized: str) -> bool:
    """Return True if ``normalized`` matches any built-in dangerous-command regex."""
    return any(pattern.search(normalized) for pattern in _DANGEROUS)


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

    return "confirm", "not on the allowlist; needs approval"
