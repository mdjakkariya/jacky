"""prompt_toolkit input: the choice value object, choice parsers, and the / + @ completer.

The pure parsers and the completer are unit-tested; :func:`make_session` / :func:`make_reader`
build the real interactive objects and are exercised by the manual smoke test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory


@dataclass(frozen=True, slots=True)
class Answer:
    """A parked-turn answer: the reply ``value`` (+ optional refine ``text``)."""

    value: str
    text: str = ""


def parse_plan_choice(raw: str) -> Answer | None:
    """Map a typed plan answer to an :class:`Answer`, or ``None`` if unrecognized."""
    low = raw.strip().lower()
    if low in ("1", "y", "yes", "approve"):
        return Answer("approve")
    if low in ("2", "e", "edit", "refine"):
        return Answer("refine")
    if low in ("3", "n", "no", "reject", "cancel"):
        return Answer("reject")
    return None


def parse_confirm_choice(raw: str) -> Answer | None:
    """Map a typed confirm answer to an :class:`Answer`, or ``None`` if unrecognized."""
    low = raw.strip().lower()
    if low in ("1", "y", "yes"):
        return Answer("yes")
    if low in ("2", "n", "no", "reject"):
        return Answer("no")
    return None


class JackCompleter(Completer):  # type: ignore[misc]
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
        try:
            entries = sorted(p.name for p in Path(self._cwd).iterdir())
        except OSError:
            return
        for name in entries:
            if name.startswith(prefix):
                yield Completion(name, start_position=-len(prefix))


def make_session(cwd: str, commands: dict[str, str]) -> PromptSession[str]:
    """Build the real interactive session (history + the / and @ completer)."""
    return PromptSession(
        completer=JackCompleter(commands, cwd),
        history=InMemoryHistory(),
        complete_while_typing=True,
    )


def make_reader(session: PromptSession[str]) -> Callable[[str], str | None]:
    """Wrap ``session`` in a ``reader(prompt) -> line | None`` (``None`` on EOF)."""

    def reader(prompt_str: str) -> str | None:
        from prompt_toolkit.formatted_text import ANSI

        try:
            return session.prompt(ANSI(f"\x1b[1;38;2;79;214;184m{prompt_str}\x1b[0m"))  # type: ignore[no-any-return]
        except EOFError:
            return None

    return reader
