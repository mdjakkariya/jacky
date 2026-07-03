"""Search/replace engine for code edits (pure, no I/O).

The engine ``edit_file`` and ``multi_edit`` use to apply one search/replace block.
It follows claude-code's ``FileEditTool`` strategy — exact matching plus a single
tolerant pass — rather than fuzzy indentation reflow, because the model reads a file
(line-numbered) right before editing, so it already has the exact text:

1. **exact** — the search text appears verbatim; it must be unique unless
   ``replace_all`` is set, in which case every occurrence is replaced.
2. **trailing whitespace** — if the exact pass finds nothing, match line-by-line after
   ``rstrip()`` (the one drift read-before-edit can't reveal, since trailing spaces are
   invisible in numbered output); this pass requires a unique match.

Guards: an empty search, an identical search/replacement, no match, or a non-unique
match all leave the content unchanged and explain why — the engine never guesses where
an edit lands. All file/permission handling lives in ``tools.py``; this module is pure
so the matcher can be tested exhaustively with plain strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Outcome of one search/replace attempt."""

    ok: bool
    """True when the search text matched uniquely (or ``replace_all``); then ``content``
    is the edited text."""
    content: str
    """The edited content when ``ok``; the original content unchanged otherwise."""
    detail: str
    """Which pass matched (e.g. ``"exact match"``) or why it failed."""


def _lines(text: str) -> list[str]:
    r"""Split on ``\n`` so that ``"\n".join(_lines(t)) == t`` round-trips exactly."""
    return text.split("\n")


def _block_lines(text: str) -> list[str]:
    r"""A search/replacement block as lines, dropping one trailing empty from a final newline.

    So ``"    return 1\n"`` and ``"    return 1"`` describe the same single line — the
    search and replacement blocks are normalized the same way, keeping line counts aligned.
    """
    ls = _lines(text)
    if ls and ls[-1] == "":
        ls = ls[:-1]
    return ls


def _rstrip_hits(c_lines: list[str], f_keys: list[str]) -> list[int]:
    """Start indices where a window of ``c_lines`` equals ``f_keys`` after ``rstrip()``."""
    win = len(f_keys)
    if win == 0 or win > len(c_lines):
        return []
    return [
        i
        for i in range(0, len(c_lines) - win + 1)
        if [c_lines[j].rstrip() for j in range(i, i + win)] == f_keys
    ]


def apply_replace(
    content: str, find: str, replace: str, *, replace_all: bool = False
) -> ReplaceResult:
    """Apply one search/replace to ``content``.

    Returns a :class:`ReplaceResult`; on any failure the original ``content`` is returned
    unchanged so the caller can leave the file untouched.
    """
    if find == "":
        return ReplaceResult(False, content, "empty search text")
    if find == replace:
        return ReplaceResult(False, content, "the search and replacement text are identical")

    # Pass 1 — exact.
    n = content.count(find)
    if n >= 1:
        if n > 1 and not replace_all:
            return ReplaceResult(
                False,
                content,
                f"the search text appears {n} times; add surrounding context to make it "
                "unique, or set replace_all to replace every occurrence",
            )
        edited = content.replace(find, replace, -1 if replace_all else 1)
        return ReplaceResult(True, edited, f"exact match ({n})" if replace_all else "exact match")

    # Pass 2 — trailing-whitespace-tolerant, unique only.
    c_lines = _lines(content)
    f_keys = [ln.rstrip() for ln in _block_lines(find)]
    if f_keys:
        hits = _rstrip_hits(c_lines, f_keys)
        if len(hits) > 1:
            return ReplaceResult(
                False,
                content,
                f"the search text matches {len(hits)} places (ignoring trailing whitespace); "
                "add surrounding context to make it unique",
            )
        if len(hits) == 1:
            i = hits[0]
            new_lines = c_lines[:i] + _block_lines(replace) + c_lines[i + len(f_keys) :]
            return ReplaceResult(True, "\n".join(new_lines), "whitespace match")

    return ReplaceResult(False, content, "search text not found")
