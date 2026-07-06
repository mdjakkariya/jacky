"""The `textual` TUI for the coding agent: transcript + docked input + status line.

A submitted line that parses as a slash command is handled inline; otherwise it drives the
coder turn on a threaded worker (the HTTP client is blocking), rendering plan/confirm/reply
segments and a git diff. ``textual``/``rich`` are imported lazily so this module (and the
non-TUI test suite) load without the ``tui`` extra.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

from autobot.cli import client, commands, gitdiff
from autobot.cli.classify import classify
from autobot.cli.render import render_diff_rich, render_plain, render_rich

_Post = Callable[[str, dict[str, Any], float], dict[str, Any]]


class JackApp(App[None]):
    """A line-oriented textual app driving the coder daemon over its turn protocol."""

    CSS = """
    #transcript { height: 1fr; }
    #status { dock: bottom; height: 1; color: $text-muted; }
    #input { dock: bottom; }
    """

    def __init__(
        self,
        base_url: str,
        cwd: str,
        *,
        post: _Post = client._post,
        snapshot: Callable[[str], str | None] = gitdiff.snapshot,
        diff_since: Callable[[str, str | None], str | None] = gitdiff.diff_since,
    ) -> None:
        """Wire the app; ``post``/``snapshot``/``diff_since`` are injectable for tests."""
        super().__init__()
        self._base_url = base_url
        self._cwd = cwd
        self._post = post
        self._snapshot = snapshot
        self._diff_since = diff_since
        self._awaiting = False  # True while a turn is parked awaiting a plan/confirm answer
        self._snap: str | None = None  # worktree snapshot taken at turn start
        self._history: list[str] = []  # plain-text mirror of the transcript (for tests/logging)

    def compose(self) -> ComposeResult:
        """Lay out the transcript, status line, and input."""
        yield RichLog(id="transcript", wrap=True, markup=False)
        yield Static(f"{self._cwd}", id="status")
        yield Input(placeholder="Type a request, or /help. Ctrl-C to quit.", id="input")

    def on_mount(self) -> None:
        """Focus the input on start."""
        self.query_one("#input", Input).focus()

    def transcript_text(self) -> str:
        """The transcript's plain-text mirror (for tests/logging)."""
        return "\n".join(self._history)

    def _emit(self, plain: str, rich_renderable: object) -> None:
        """Append plain text to the history and write the rich renderable to the log."""
        self._history.append(plain)
        self.query_one("#transcript", RichLog).write(rich_renderable)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle a submitted line: slash command, an awaited answer, or a new turn."""
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        if self._awaiting:
            self._answer(text)
            return
        parsed = commands.parse(text)
        if parsed is not None:
            self._run_command(*parsed)
            return
        self._emit(f"> {text}", f"> {text}")
        self._snap = self._snapshot(self._cwd)
        self._start(text)

    def _run_command(self, name: str, args: str) -> None:
        res = commands.dispatch(name, args)
        if res.action == "exit":
            self.exit()
        elif res.action == "clear":
            self.query_one("#transcript", RichLog).clear()
            self._history.clear()
        else:
            self._emit(res.text, res.text)

    @work(thread=True)
    def _start(self, text: str) -> None:
        resp = client.start_turn(self._base_url, text, post=self._post)
        self.call_from_thread(self._render_response, resp)

    def _answer(self, line: str) -> None:
        value, extra = self._interpret_answer(line)
        self._awaiting = False
        self._answer_worker(value, extra)

    @work(thread=True)
    def _answer_worker(self, value: str, extra: str) -> None:
        resp = client.answer(self._base_url, value, extra, post=self._post)
        self.call_from_thread(self._render_response, resp)

    def _interpret_answer(self, line: str) -> tuple[str, str]:
        """Map a typed answer to a (value, text) for /coder/reply."""
        low = line.strip().lower()
        if low in ("y", "yes", "approve"):
            return "approve", ""
        if low in ("n", "no", "reject"):
            return "reject", ""
        if low in ("e", "edit"):
            return "refine", ""  # refine with no text re-plans as-is; text refine is a later polish
        return "refine", line

    def _render_response(self, resp: dict[str, Any] | str) -> None:
        """Render a daemon response; enter awaiting state for plan/pending."""
        if isinstance(resp, str):  # transport/JSON error already made friendly
            self._emit(resp, resp)
            return
        seg = classify(resp)
        self._emit(render_plain(seg), render_rich(seg))
        if seg.kind in ("plan", "pending"):
            self._awaiting = True
            return
        if seg.kind == "done":
            diff = self._diff_since(self._cwd, self._snap)
            if diff:
                self._emit(diff, render_diff_rich(diff))


def run(base_url: str, cwd: str) -> None:  # pragma: no cover - launches the interactive app
    """Launch the TUI against ``base_url`` for the project at ``cwd``."""
    JackApp(base_url, cwd).run()
