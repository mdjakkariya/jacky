"""Spawn the real `jack` TUI on a PTY and observe it through a `pyte` screen.

The reader thread drains the PTY master into a ``pyte`` stream so ``screen_text()`` is the
2-D render a human would see. ``wait_for`` polls that render for a marker (no fixed sleeps).
``_for_test`` builds a session with no process, so the wait loop and rendering unit-test by
feeding canned bytes; the real spawn is exercised by ``make e2e`` (dogfooding).
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autobot.e2e.markers import Marker
from autobot.logging_setup import get_logger

_log = get_logger("e2e")

_KEYS = {"enter": "\r", "esc": "\x1b", "tab": "\t"}


def jack_argv(port: int) -> list[str]:
    """Argv to launch the real `jack` TUI: the console script next to this interpreter."""
    jack = Path(sys.executable).with_name("jack")
    if jack.exists():
        return [str(jack), "--port", str(port)]
    # Fallback: run the CLI entrypoint via the same interpreter.
    return [
        sys.executable,
        "-c",
        "from autobot.cli import main; import sys; sys.exit(main())",
        "--port",
        str(port),
    ]


class PtySession:
    """A running (or headless test) TUI attached to a pseudo-terminal + pyte screen."""

    def __init__(
        self,
        *,
        cols: int,
        rows: int,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Init the pyte screen + timing seams; use :meth:`spawn` / :meth:`_for_test`."""
        import pyte

        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._raw = bytearray()
        self._lock = threading.Lock()
        self._now = now
        self._sleep = sleep
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._alive = False

    @classmethod
    def _for_test(cls, *, cols: int, rows: int, **kw: Any) -> PtySession:
        """A session with no process — feed bytes with :meth:`feed`."""
        return cls(cols=cols, rows=rows, **kw)

    @classmethod
    def spawn(cls, argv: list[str], cwd: str, *, cols: int = 100, rows: int = 40) -> PtySession:
        """Fork ``argv`` on a new PTY at ``cwd`` and start draining it."""
        self = cls(cols=cols, rows=rows)
        master, slave = os.openpty()
        env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
        self._proc = subprocess.Popen(  # the real jack TUI
            argv,
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self._master_fd = master
        self._alive = True
        self._reader = threading.Thread(target=self._drain, name="e2e-pty-reader", daemon=True)
        self._reader.start()
        _log.info("pty spawned pid=%s cwd=%s", self._proc.pid, cwd)
        return self

    def _drain(self) -> None:
        assert self._master_fd is not None
        while self._alive:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.1)
                if not r:
                    continue
                data = os.read(self._master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            self.feed(data)

    def feed(self, data: bytes) -> None:
        """Feed raw bytes into the pyte screen (reader thread, or tests)."""
        with self._lock:
            self._raw.extend(data)
            self._stream.feed(data)

    def screen_text(self) -> str:
        """The current rendered screen as text (one line per row, trailing blanks trimmed)."""
        with self._lock:
            lines = [self._screen.display[i].rstrip() for i in range(self._screen.lines)]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def raw_bytes(self) -> bytes:
        """The full raw byte transcript so far."""
        with self._lock:
            return bytes(self._raw)

    def send(self, text: str) -> None:
        """Type ``text`` then Enter into the terminal."""
        self._write((text + "\r").encode("utf-8"))

    def send_key(self, name: str) -> None:
        """Press a named key (``enter``/``esc``/``tab``) or a literal character."""
        self._write(_KEYS.get(name, name).encode("utf-8"))

    def _write(self, data: bytes) -> None:
        if self._master_fd is not None:
            os.write(self._master_fd, data)

    def wait_for(self, marker: Marker, timeout: float, poll: float = 0.05) -> bool:
        """Poll the rendered screen until ``marker`` holds, or ``timeout`` elapses."""
        deadline = self._now() + timeout
        while self._now() < deadline:
            if marker(self.screen_text()):
                return True
            self._sleep(poll)
        return marker(self.screen_text())

    def wait_until_stable(
        self, marker: Marker, timeout: float, *, stable_for: float = 1.0, poll: float = 0.05
    ) -> bool:
        """Poll until ``marker`` holds *continuously* for ``stable_for`` seconds (debounced).

        A single satisfying frame is not enough: the TUI paints replies and spinners in
        transient ``rich.Live`` regions that briefly clear between phases, so the idle
        ``❯`` prompt underneath flickers into view mid-turn. Requiring the marker to hold
        across a settle window rejects those flickers and only fires on a real resting state.
        """
        deadline = self._now() + timeout
        held_since: float | None = None
        while True:
            now = self._now()
            if now >= deadline:
                return False
            if marker(self.screen_text()):
                if held_since is None:
                    held_since = now
                elif now - held_since >= stable_for:
                    return True
            else:
                held_since = None  # a flicker resets the settle window
            self._sleep(poll)

    def close(self) -> None:
        """Stop the reader and terminate the process group."""
        self._alive = False
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self._proc.kill()
                self._proc.wait(timeout=5)
        if self._master_fd is not None:
            os.close(self._master_fd)
            self._master_fd = None
