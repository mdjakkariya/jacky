"""prompt_toolkit building blocks: the choice value object, single-key chooser, and completer.

The pure parsers and the ``/`` + ``@`` completer are unit-tested; the single-key chooser
(:func:`read_choice`) is driven headlessly with a pipe input. The interactive shell itself
lives in :mod:`autobot.cli.app` (one long-lived Application); this module only owns the
reusable input pieces it composes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


#: One selectable option in a single-key choice: (key, label, value).
Choice = tuple[str, str, str]

#: A single-key chooser: (body, options) -> the pressed option's value ("" = cancel).
KeyChooser = Callable[[str, list[Choice]], str]


def read_choice(body: str, options: list[Choice]) -> str:
    """Show ``body`` + a single-key affordance, transiently, and return the pressed value.

    A single keypress resolves it (no Enter); the prompt renders through a prompt_toolkit
    ``Application`` with ``erase_when_done`` so it vanishes from the scrollback once answered,
    keeping the history clean. ``options`` is a list of ``(key, label, value)``; Ctrl-C /
    Escape / EOF return ``""`` (cancel). An unrecognized key is ignored (keeps waiting), so a
    stray press can't mis-answer.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    affordance = "   ".join(f"({key}) {label}" for key, label, _ in options)
    body_lines = body.rstrip("\n").split("\n")
    # prompt_toolkit's (style, text) fragment list; list[Any] to satisfy its union element type.
    fragments: list[Any] = [("", line + "\n") for line in body_lines]
    fragments.append(("class:affordance", affordance))
    window = Window(
        FormattedTextControl(fragments), height=len(body_lines) + 1, always_hide_cursor=True
    )

    kb = KeyBindings()

    def _bind(value: str) -> Callable[[object], None]:
        def handler(event: object) -> None:
            event.app.exit(result=value)  # type: ignore[attr-defined]

        return handler

    for key, _label, value in options:
        kb.add(key)(_bind(value))
    kb.add("c-c")(_bind(""))
    kb.add("escape")(_bind(""))

    app: Application[str] = Application(
        layout=Layout(window),
        key_bindings=kb,
        erase_when_done=True,
        style=Style.from_dict({"affordance": "#4fd6b8 bold"}),
    )
    try:
        result = app.run()
    except (EOFError, KeyboardInterrupt):
        return ""
    return result if isinstance(result, str) else ""
