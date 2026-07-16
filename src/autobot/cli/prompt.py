"""prompt_toolkit building blocks: the parked-turn ``Answer`` and the ``/`` + ``@`` completer.

The ``Answer`` value object and the completer are unit-tested. The interactive shell itself
lives in :mod:`autobot.cli.app` (one long-lived Application); this module only owns the
reusable input pieces it composes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True, slots=True)
class Answer:
    """A parked-turn answer: the reply ``value`` (+ optional refine ``text``)."""

    value: str
    text: str = ""


class JackCompleter(Completer):
    """Completes ``/command`` at the line start and ``@path`` for the current word."""

    def __init__(self, commands: dict[str, str], cwd: str) -> None:
        """Store the command set and the directory ``@`` paths are resolved against."""
        self._commands = commands
        self._cwd = cwd

    def get_completions(  # noqa: D102
        self, document: Document, complete_event: object
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            yield from self._slash(text)
            return
        word = text.rsplit(" ", 1)[-1]
        if word.startswith("@"):
            yield from self._files(word[1:])

    def _slash(self, text: str) -> Iterable[Completion]:
        for name, desc in self._commands.items():
            if name.startswith(text):
                yield Completion(name, start_position=-len(text), display_meta=desc)

    def _files(self, prefix: str) -> Iterable[Completion]:
        """Complete an ``@path`` segment, descending into subfolders, with type icons.

        ``prefix`` is the text after ``@`` (e.g. ``src/cli/pro``). Only the final segment is
        completed (``pro`` -> ``prompt.py``); folders gain a trailing ``/`` so the next
        keystroke keeps descending. Folders sort first, then files, each shown with a glyph
        and a type in the meta column.
        """
        dirpart, _, partial = prefix.rpartition("/")
        base = Path(self._cwd) / dirpart if dirpart else Path(self._cwd)
        try:
            entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if not entry.name.startswith(partial):
                continue
            is_dir = entry.is_dir()
            icon, kind = _icon_for(entry, is_dir=is_dir)
            suffix = "/" if is_dir else ""
            yield Completion(
                entry.name + suffix,
                start_position=-len(partial),
                display=f"{icon} {entry.name}{suffix}",
                display_meta=kind,
            )


# Type glyphs by extension for the @-path completer (folder handled separately). Terminal
# emoji, so any font renders them; the meta column carries the word for clarity.
_ICONS: dict[str, tuple[str, str]] = {
    ".png": ("🖼️", "image"),
    ".jpg": ("🖼️", "image"),
    ".jpeg": ("🖼️", "image"),
    ".gif": ("🖼️", "image"),
    ".webp": ("🖼️", "image"),
    ".svg": ("🖼️", "image"),
    ".bmp": ("🖼️", "image"),
    ".ico": ("🖼️", "image"),
    ".pdf": ("📕", "pdf"),
    ".doc": ("📘", "doc"),
    ".docx": ("📘", "doc"),
    ".xls": ("📊", "sheet"),
    ".xlsx": ("📊", "sheet"),
    ".csv": ("📊", "sheet"),
    ".md": ("📝", "markdown"),
    ".json": ("🔧", "config"),
    ".yaml": ("🔧", "config"),
    ".yml": ("🔧", "config"),
    ".toml": ("🔧", "config"),
}


def _icon_for(path: Path, *, is_dir: bool) -> tuple[str, str]:
    """The (glyph, type-word) for a completion entry."""
    if is_dir:
        return ("📁", "folder")
    return _ICONS.get(path.suffix.lower(), ("📄", "file"))
