"""End-to-end auto-resume: a real backgrounded subprocess's completion over a real socket.

Exercises the whole Phase-1/Phase-2 transport with no fakes on the path that matters: a real
``run_command(run_in_background=True)`` spawns a real ``echo`` subprocess; when it exits, the
real ``TaskRegistry`` listener fires; a real uvicorn daemon streams the settle event over a
real ``GET /coder/events`` SSE socket; and the real ``client.stream_events`` (urllib) parses
it — the exact path an idle CLI uses to auto-resume. Also asserts the completion note landed
in the inbox and the output was streamed to the managed log file.

Skipped unless uvicorn (a base dep since #96) is installed. Run with:
    uv run pytest tests/integration/test_autoresume_e2e.py -v
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

from autobot.cli import client  # noqa: E402, RUF100
from autobot.core.events import EventBus  # noqa: E402, RUF100
from autobot.core.streaming import active_session_id  # noqa: E402, RUF100
from autobot.daemon.server import create_app  # noqa: E402, RUF100
from autobot.tasks import NotificationInbox, Task, TaskRegistry  # noqa: E402, RUF100
from autobot.tools.access import AccessBroker, AccessPolicy  # noqa: E402, RUF100
from autobot.tools.code.shell import run_command  # noqa: E402, RUF100


class _GrantAll:
    """A confirmer that approves everything, so the real run_command executes."""

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_backgrounded_command_completion_streams_over_a_real_socket(tmp_path: Path) -> None:
    import uvicorn

    reg = TaskRegistry()
    inbox = NotificationInbox()

    # Mirror the orchestrator's Task->event mapping; flag when the SSE handler has subscribed
    # so we only fire the command once the listener is registered (no lost event).
    subscribed = threading.Event()

    def subscribe(cb: Any) -> Any:
        def on_task(task: Task) -> None:
            cb(
                {
                    "type": "task",
                    "id": task.id,
                    "status": task.status,
                    "returncode": task.returncode,
                }
            )

        unsub = reg.add_listener(on_task)
        subscribed.set()
        return unsub

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(EventBus(), on_coder_events=subscribe)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        timeout_graceful_shutdown=1,  # don't hang on the open SSE connection at teardown
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, name="e2e-daemon", daemon=True)
    server_thread.start()

    received: list[dict[str, Any]] = []

    def consume() -> None:
        for evt in client.stream_events(base_url):  # real urllib GET over the socket
            received.append(evt)

    consumer = threading.Thread(target=consume, name="e2e-consumer", daemon=True)

    try:
        assert _wait(lambda: client.is_daemon_up(base_url), 15.0), "daemon never came up"
        consumer.start()
        assert subscribed.wait(10.0), "the SSE stream never subscribed"

        # Fire a REAL backgrounded command (real /bin/sh subprocess via the default runner).
        broker = AccessBroker(
            AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path),
            _GrantAll(),
        )
        token = active_session_id.set("e2e-session")
        try:
            reply = run_command(
                "echo hello-from-bg",
                broker,
                str(tmp_path),
                run_in_background=True,
                registry=reg,
                inbox=inbox,
            )
        finally:
            active_session_id.reset(token)
        assert "background" in reply.lower() and "task-1" in reply

        # 1) The settle event reached the client over the real HTTP socket (auto-resume path).
        got_event = _wait(lambda: any(e.get("id") == "task-1" for e in received), 15.0)
        assert got_event, f"no task event over the wire; received={received}"
        evt = next(e for e in received if e.get("id") == "task-1")
        assert evt["status"] == "done" and evt["returncode"] == 0

        # 2) The completion note landed in the session inbox (what the harness folds next turn).
        assert _wait(lambda: inbox.pending("e2e-session") > 0, 5.0), "inbox note never arrived"
        note = inbox.drain("e2e-session")[0]
        assert "task-1" in note and "hello-from-bg" in note

        # 3) The output was streamed to the managed log file.
        log = tmp_path / ".jack" / "tasks" / "task-1.log"
        assert _wait(log.exists, 5.0), "log file never written"
        assert "hello-from-bg" in log.read_text()
    finally:
        server.should_exit = True
        server.force_exit = True  # drop the open SSE connection immediately
        server_thread.join(timeout=10.0)
