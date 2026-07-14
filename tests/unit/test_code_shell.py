"""Tests for the code execution tool (run_command) — runner injected, no real process."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from autobot.core.streaming import active_session_id
from autobot.tasks import NotificationInbox, Task, TaskRegistry
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.shell import CommandRunner, run_command


class _FakeConfirmer:
    def __init__(self, grant: bool) -> None:
        self._grant = grant

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return self._grant

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default if self._grant else ""


def _broker(tmp_path: Path, *, grant: bool = True) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer(grant))


def _fake_runner(rc: int, out: str, timed_out: bool = False) -> CommandRunner:
    def run(
        command: str, cwd: str, timeout: float, on_output: Callable[[str], None] | None = None
    ) -> tuple[int, str, bool]:
        if on_output is not None:
            for line in out.splitlines():
                on_output(line)
        return rc, out, timed_out

    return run


def test_run_command_success(tmp_path: Path) -> None:
    out = run_command("echo hi", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, "hi\n"))
    assert "hi" in out
    assert "ok" in out.lower()


def test_run_command_nonzero_exit_shows_status(tmp_path: Path) -> None:
    out = run_command("false", _broker(tmp_path), str(tmp_path), runner=_fake_runner(1, "boom\n"))
    assert "exit 1" in out
    assert "boom" in out


def test_run_command_timeout(tmp_path: Path) -> None:
    out = run_command(
        "sleep 999", _broker(tmp_path), str(tmp_path), runner=_fake_runner(124, "partial", True)
    )
    assert "timed out" in out.lower()
    assert "partial" in out


def test_run_command_large_output_is_budgeted(tmp_path: Path) -> None:
    big = "\n".join(f"row {i}" for i in range(5_000))
    out = run_command(
        "gen",
        _broker(tmp_path),
        str(tmp_path),
        runner=_fake_runner(0, big),
        output_model_cap=2_000,
    )
    assert ".jack/command-output/" in out  # full output spilled to disk
    assert "row 4999" in out  # tail preserved
    assert len(out) < 3_000  # model-facing result bounded


def test_run_command_streams_lines_to_sink(tmp_path: Path) -> None:
    from autobot.core.streaming import output_sink

    seen: list[str] = []
    token = output_sink.set(seen.append)
    try:
        run_command("gen", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, "a\nb\nc\n"))
    finally:
        output_sink.reset(token)
    assert seen == ["a", "b", "c"]  # each line streamed live to the human


def test_run_command_empty(tmp_path: Path) -> None:
    out = run_command("   ", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, ""))
    assert "command" in out.lower()


def test_run_command_denied_when_not_granted(tmp_path: Path) -> None:
    out = run_command(
        "echo hi", _broker(tmp_path, grant=False), str(tmp_path), runner=_fake_runner(0, "hi\n")
    )
    assert "don't have access" in out.lower()


def test_run_command_timeout_is_clamped(tmp_path: Path) -> None:
    # A caller asking for 10_000s must be clamped to the max; the runner sees the clamp.
    seen: list[float] = []

    def run(
        command: str, cwd: str, timeout: float, on_output: Callable[[str], None] | None = None
    ) -> tuple[int, str, bool]:
        seen.append(timeout)
        return 0, "ok", False

    run_command("echo hi", _broker(tmp_path), str(tmp_path), timeout=10_000.0, runner=run)
    assert seen == [600.0]


def test_run_command_blocks_dangerous_command_without_running_it(tmp_path: Path) -> None:
    calls: list[str] = []

    def run(
        command: str, cwd: str, timeout: float, on_output: Callable[[str], None] | None = None
    ) -> tuple[int, str, bool]:
        calls.append(command)
        return 0, "should not run", False

    out = run_command("rm -rf /", _broker(tmp_path), str(tmp_path), runner=run)
    assert "blocked" in out.lower()
    assert calls == []  # the runner was never invoked


def test_run_command_runs_normally_with_empty_allow_and_blocklists(tmp_path: Path) -> None:
    out = run_command(
        "echo hi",
        _broker(tmp_path),
        str(tmp_path),
        runner=_fake_runner(0, "hi\n"),
        allowlist=[],
        blocklist=[],
    )
    assert "hi" in out
    assert "ok" in out.lower()


def _wait_settled(reg: TaskRegistry, task_id: str, timeout: float = 2.0) -> Task:
    """Block until the background worker thread has settled ``task_id`` (or fail)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = reg.get(task_id)
        if task is not None and task.settled:
            return task
        time.sleep(0.01)
    raise AssertionError(f"background task {task_id} did not settle in {timeout}s")


def _run_background(
    tmp_path: Path, runner: CommandRunner, *, session_id: str = "sess-1"
) -> tuple[str, TaskRegistry, NotificationInbox]:
    """Run ``run_command`` in background mode with the session-id seam set, like a turn."""
    reg = TaskRegistry()
    inbox = NotificationInbox()
    token = active_session_id.set(session_id)
    try:
        reply = run_command(
            "npx playwright test",
            _broker(tmp_path),
            str(tmp_path),
            runner=runner,
            run_in_background=True,
            registry=reg,
            inbox=inbox,
        )
    finally:
        active_session_id.reset(token)
    return reply, reg, inbox


def test_run_in_background_returns_immediately_and_registers_task(tmp_path: Path) -> None:
    reply, reg, _inbox = _run_background(tmp_path, _fake_runner(0, "3 passed\n"))
    assert "background" in reply.lower()
    assert "task-1" in reply
    task = reg.get("task-1")
    assert task is not None
    assert task.session_id == "sess-1"
    assert task.kind == "command"


def test_run_in_background_success_notifies_and_logs(tmp_path: Path) -> None:
    _reply, reg, inbox = _run_background(tmp_path, _fake_runner(0, "3 passed\n"))
    task = _wait_settled(reg, "task-1")
    assert task.status == "done"
    assert task.returncode == 0
    notes = inbox.drain("sess-1")
    assert len(notes) == 1
    assert "task-1" in notes[0] and "exit 0" in notes[0] and "3 passed" in notes[0]
    # Full output streamed to the managed log file.
    log = (tmp_path / ".jack" / "tasks" / "task-1.log").read_text()
    assert "3 passed" in log


def test_run_in_background_failure_marks_failed_and_notifies(tmp_path: Path) -> None:
    _reply, reg, inbox = _run_background(tmp_path, _fake_runner(1, "1 failed\n"))
    task = _wait_settled(reg, "task-1")
    assert task.status == "failed"
    assert task.returncode == 1
    assert "exit 1" in inbox.drain("sess-1")[0]


def test_run_in_background_timeout_marks_failed(tmp_path: Path) -> None:
    _reply, reg, inbox = _run_background(tmp_path, _fake_runner(124, "partial", True))
    task = _wait_settled(reg, "task-1")
    assert task.status == "failed"
    assert task.returncode == 124
    assert "timed out" in inbox.drain("sess-1")[0].lower()


def test_run_in_background_falls_back_to_foreground_without_registry(tmp_path: Path) -> None:
    # No registry/inbox wired: the request degrades to a normal foreground run, not an error.
    out = run_command(
        "echo hi",
        _broker(tmp_path),
        str(tmp_path),
        runner=_fake_runner(0, "hi\n"),
        run_in_background=True,
    )
    assert "hi" in out
    assert "ok" in out.lower()
    assert "background" not in out.lower()


def test_run_command_blocks_command_matching_user_blocklist(tmp_path: Path) -> None:
    calls: list[str] = []

    def run(
        command: str, cwd: str, timeout: float, on_output: Callable[[str], None] | None = None
    ) -> tuple[int, str, bool]:
        calls.append(command)
        return 0, "should not run", False

    out = run_command(
        "npm publish",
        _broker(tmp_path),
        str(tmp_path),
        runner=run,
        blocklist=["npm publish"],
    )
    assert "blocked" in out.lower()
    assert calls == []
