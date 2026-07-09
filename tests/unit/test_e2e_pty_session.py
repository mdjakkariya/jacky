"""PtySession marker-wait loop + screen rendering (no real process)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from autobot.e2e import markers
from autobot.e2e.pty_session import PtySession, jack_argv


def _headless() -> PtySession:
    # Build a session with no real process — feed bytes directly into the pyte screen.
    return PtySession._for_test(cols=40, rows=10)


def test_feed_renders_screen() -> None:
    s = _headless()
    s.feed(b"hello ")
    s.feed(b"world")
    assert "hello world" in s.screen_text()


def test_wait_for_returns_true_when_marker_appears() -> None:
    ticks = iter([0.0, 0.1, 0.2, 0.3])
    fed = {"n": 0}

    def sleep(_dt: float) -> None:
        fed["n"] += 1
        if fed["n"] == 2:
            s.feed(b"\r\n\xe2\x8f\xba done")  # "⏺ done"

    s = PtySession._for_test(cols=40, rows=10, now=lambda: next(ticks), sleep=sleep)
    assert s.wait_for(markers.reply_present, timeout=1.0) is True


def test_wait_for_times_out() -> None:
    ticks = iter([0.0, 0.5, 1.0, 1.5])
    s = PtySession._for_test(cols=40, rows=10, now=lambda: next(ticks), sleep=lambda _dt: None)
    assert s.wait_for(markers.reply_present, timeout=1.0) is False


def test_jack_argv_uses_console_script() -> None:
    argv = jack_argv(8790)
    assert argv[-2:] == ["--port", "8790"] and argv[0].endswith("jack")
