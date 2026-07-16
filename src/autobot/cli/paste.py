"""Large-paste handling for the pinned input: collapse to a placeholder, expand on send.

A big paste is stashed under an id and shown in the input as a compact placeholder
(``[Pasted #1 · 23 lines]``); Backspace over a placeholder drops the whole block; on send the
placeholders are expanded back to their real content. A pasted existing path is turned into an
``@mention`` instead (reusing the file-attachment path). Image paste is not handled yet — the
turn protocol is text-only.

Pure and dependency-free (no prompt_toolkit), so it unit-tests without a terminal; the app
(:mod:`autobot.cli.app`) wires it into the bracketed-paste and Backspace key bindings.
"""

from __future__ import annotations

import re
from pathlib import Path

PASTE_LINE_THRESHOLD = 11  # collapse a paste with at least this many lines (bigger than the
PASTE_CHAR_THRESHOLD = 1000  # input box's growth cap), …or at least this many characters.

# The placeholder token shown in the input for a stashed paste. Its exact shape (with the
# ``·`` separator) makes it safe to match/strip and unlikely to be typed by hand.
_PLACEHOLDER_RE = re.compile(r"\[Pasted #(\d+) · [^\]]+\]")


def should_collapse(text: str) -> bool:
    """Whether a pasted blob is large enough to stash behind a placeholder."""
    return text.count("\n") + 1 >= PASTE_LINE_THRESHOLD or len(text) >= PASTE_CHAR_THRESHOLD


def summary(text: str) -> str:
    """A short human summary of a paste: ``23 lines`` (multi-line) or ``512 chars`` (one line)."""
    return f"{text.count(chr(10)) + 1} lines" if "\n" in text else f"{len(text)} chars"


def placeholder(n: int, text: str) -> str:
    """The placeholder token for paste ``n``."""
    return f"[Pasted #{n} · {summary(text)}]"


def is_existing_path(text: str, cwd: str) -> str | None:
    """Return the (stripped) path if ``text`` is a single-line existing file/dir, else ``None``.

    Relative paths are resolved against ``cwd`` for the existence check, but the original
    (as-pasted) string is returned so the mention is inserted verbatim.
    """
    s = text.strip()
    if not s or "\n" in s:
        return None
    p = Path(s).expanduser()
    candidate = p if p.is_absolute() else Path(cwd) / p
    return s if candidate.exists() else None


def trailing_placeholder(text_before_cursor: str) -> str | None:
    """The placeholder token ending exactly at the cursor, if any (for atomic Backspace)."""
    matches = list(_PLACEHOLDER_RE.finditer(text_before_cursor))
    if matches and matches[-1].end() == len(text_before_cursor):
        return matches[-1].group(0)
    return None


class PasteStore:
    """Stashes large pastes behind placeholder tokens; expands them back on send."""

    def __init__(self) -> None:
        """Start empty; ids count up from 1."""
        self._by_id: dict[int, str] = {}
        self._next = 1

    def add(self, text: str) -> str:
        """Stash ``text`` and return its placeholder token."""
        n = self._next
        self._next += 1
        self._by_id[n] = text
        return placeholder(n, text)

    def forget(self, token: str) -> None:
        """Drop the paste a placeholder ``token`` refers to (called on atomic delete)."""
        match = _PLACEHOLDER_RE.fullmatch(token)
        if match is not None:
            self._by_id.pop(int(match.group(1)), None)

    def expand(self, text: str) -> str:
        """Replace every known placeholder token in ``text`` with its stashed content.

        An unknown token (e.g. recalled from a prior session's history) is left as-is.
        """

        def _sub(match: re.Match[str]) -> str:
            return self._by_id.get(int(match.group(1)), match.group(0))

        return _PLACEHOLDER_RE.sub(_sub, text)
