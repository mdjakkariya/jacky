"""The background-events listener: collects finished tasks and wakes the idle prompt."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from autobot.cli.autoresume import BackgroundEvents


def test_collects_completed_tasks_and_fires_the_waker() -> None:
    open_forever = threading.Event()

    def fake_stream(_base_url: str) -> Iterator[dict[str, Any]]:
        yield {"type": "task", "id": "task-1", "status": "running"}  # not settled → ignored
        yield {"type": "task", "id": "task-1", "status": "done"}
        yield {"type": "other"}  # non-task → ignored
        open_forever.wait(2.0)  # hold the stream open so it doesn't reconnect and re-yield

    woke = threading.Event()
    be = BackgroundEvents("http://x", stream_events=fake_stream)
    be.set_waker(woke.set)
    be.start()
    try:
        assert woke.wait(2.0)  # the settle event fired the waker
        assert be.pending() is True
        done = be.poll_completed()
        assert [e["id"] for e in done] == ["task-1"]
        assert done[0]["status"] == "done"
        assert be.poll_completed() == []  # drained
    finally:
        be.close()
        open_forever.set()


def test_listener_survives_a_stream_error() -> None:
    # A stream that raises must not crash the listener thread; close() still returns.
    def boom(_base_url: str) -> Iterator[dict[str, Any]]:
        raise OSError("dropped")
        yield  # pragma: no cover - unreachable; makes this a generator

    be = BackgroundEvents("http://x", stream_events=boom)
    be.start()
    be.close()  # no exception, thread winds down
    assert be.poll_completed() == []
