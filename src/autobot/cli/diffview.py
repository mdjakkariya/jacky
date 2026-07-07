"""Render a unified diff as full-width colored bars with line numbers + word highlights.

Parsing tracks the new-file line number per row (from the ``@@`` hunk header); rendering
pads each row to a fixed width so the add/removed background bar fills the column, and
pairs adjacent removed/added runs for word-level intra-line highlighting.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text

from autobot.cli import theme

# Row prefix is a fixed-width gutter + sigil + space: "NNNN " (5) + sigil (1) + " " (1).
_PREFIX_LEN = 7


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One parsed diff line: its kind, the new-file line number (if any), and its text."""

    kind: str  # "add" | "del" | "ctx" | "hunk" | "file"
    line_no: int | None
    text: str


def parse_diff(diff: str) -> list[DiffRow]:
    """Parse a unified diff into rows, tracking the new-file line number.

    Degrades gracefully on malformed input: unparsable hunk headers reset the running
    line counter to 0 rather than raising.
    """
    rows: list[DiffRow] = []
    new_no = 0
    for line in diff.splitlines():
        if line.startswith("diff --git") or line.startswith("+++ "):
            rows.append(DiffRow("file", None, line))
        elif line.startswith(("index ", "--- ")):
            continue  # metadata we don't render
        elif line.startswith("@@"):
            new_no = _hunk_new_start(line)
            rows.append(DiffRow("hunk", None, line))
        elif line.startswith("+"):
            rows.append(DiffRow("add", new_no, line[1:]))
            new_no += 1
        elif line.startswith("-"):
            rows.append(DiffRow("del", None, line[1:]))
        else:  # context (leading space) or blank
            rows.append(DiffRow("ctx", new_no, line[1:] if line else ""))
            new_no += 1
    return rows


def _hunk_new_start(header: str) -> int:
    """Parse the new-file start line from ``@@ -a,b +c,d @@`` (defaults to 0)."""
    try:
        plus = header.split("+", 1)[1]
        return int(plus.split(",", 1)[0].split(" ", 1)[0])
    except (IndexError, ValueError):
        return 0


def _filename(rows: list[DiffRow]) -> str:
    """Best-effort target filename from the ``+++`` row, else a generic fallback."""
    for r in rows:
        if r.kind == "file" and r.text.startswith("+++ "):
            return r.text[4:].removeprefix("b/")
    return "changes"


def render_diff(diff: str, *, width: int = 80) -> Group:
    """Render ``diff`` as a rich :class:`Group` of full-width, colored, numbered rows."""
    rows = parse_diff(diff)
    adds = sum(1 for r in rows if r.kind == "add")
    dels = sum(1 for r in rows if r.kind == "del")
    lines: list[Text] = [_header(_filename(rows), adds, dels, width)]

    pending_del: list[int] = []  # indices into `lines` of a run of not-yet-paired del rows
    del_codes: list[str] = []  # the corresponding del row texts
    for r in rows:
        if r.kind in ("file", "hunk"):
            pending_del, del_codes = [], []
            continue
        if r.kind == "del":
            pending_del.append(len(lines))
            del_codes.append(r.text)
            lines.append(_code_line("-", None, r.text, "diff_del", width))
        elif r.kind == "add":
            lines.append(_code_line("+", r.line_no, r.text, "diff_add", width))
            if pending_del:
                _apply_word_highlight(lines, pending_del.pop(0), del_codes.pop(0), r.text)
        else:  # ctx
            pending_del, del_codes = [], []
            lines.append(_code_line(" ", r.line_no, r.text, "dim", width))
    return Group(*lines)


def _header(name: str, adds: int, dels: int, width: int) -> Text:
    """The full-width title bar: filename plus a +adds/-dels summary."""
    t = Text()
    t.append(f"{theme.RULE_CHAR} {name} ", style="teal")
    t.append(f"+{adds}", style="green")
    t.append(" ")
    t.append(f"-{dels}", style="red")
    fill = max(0, width - len(name) - len(f"+{adds} -{dels}") - 4)
    t.append(" " + theme.RULE_CHAR * fill, style="dim")
    return t


def _gutter(line_no: int | None) -> str:
    """The 5-char line-number column: right-justified number, or blank when unnumbered."""
    return f"{line_no:>4} " if line_no is not None else "     "


def _code_line(sigil: str, line_no: int | None, code: str, style: str, width: int) -> Text:
    """One padded row: numbered gutter + sigil in ``num`` style, code in ``style``."""
    prefix = _gutter(line_no) + sigil + " "
    body = (prefix + code).ljust(width)
    t = Text(body[: len(prefix)], style="num")
    t.append(Text(body[len(prefix) :], style=style))
    return t


def _apply_word_highlight(lines: list[Text], del_idx: int, del_code: str, add_code: str) -> None:
    """Brighten the changed words in a paired removed/added line.

    ``lines[del_idx]`` is the already-rendered removed row; the added row is always the
    last entry appended to ``lines`` (index ``len(lines) - 1``).
    """
    add_idx = len(lines) - 1
    sm = difflib.SequenceMatcher(a=del_code, b=add_code, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            lines[del_idx].stylize("diff_del_word", _PREFIX_LEN + i1, _PREFIX_LEN + i2)
        if j2 > j1:
            lines[add_idx].stylize("diff_add_word", _PREFIX_LEN + j1, _PREFIX_LEN + j2)
