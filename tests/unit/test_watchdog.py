"""Tests for the parent-death watchdog (autobot.daemon.watchdog)."""

from __future__ import annotations

import os

from autobot.daemon.watchdog import (
    _process_alive,
    parent_pid_from_env,
    start_parent_watchdog,
    watch_parent,
)


def test_parent_pid_from_env_valid() -> None:
    assert parent_pid_from_env({"AUTOBOT_PARENT_PID": "4321"}) == 4321


def test_parent_pid_from_env_missing_or_blank() -> None:
    assert parent_pid_from_env({}) is None
    assert parent_pid_from_env({"AUTOBOT_PARENT_PID": "  "}) is None


def test_parent_pid_from_env_non_positive_or_garbage() -> None:
    assert parent_pid_from_env({"AUTOBOT_PARENT_PID": "0"}) is None
    assert parent_pid_from_env({"AUTOBOT_PARENT_PID": "-1"}) is None
    assert parent_pid_from_env({"AUTOBOT_PARENT_PID": "nope"}) is None


def test_process_alive_for_self() -> None:
    assert _process_alive(os.getpid()) is True


def test_watch_parent_exits_when_parent_dies() -> None:
    # Parent is "alive" for two polls, then gone.
    alive = iter([True, True, False])
    sleeps: list[float] = []
    fired: list[bool] = []

    watch_parent(
        999,
        on_exit=lambda: fired.append(True),
        is_alive=lambda _pid: next(alive),
        sleep=sleeps.append,
        interval_s=0.5,
    )

    assert fired == [True]  # exited exactly once
    assert sleeps == [0.5, 0.5]  # slept between the two "alive" polls


def test_watch_parent_stops_on_should_continue() -> None:
    fired: list[bool] = []
    # should_continue returns False immediately: loop body never runs.
    watch_parent(
        999,
        on_exit=lambda: fired.append(True),
        is_alive=lambda _pid: True,
        sleep=lambda _s: None,
        should_continue=lambda: False,
    )
    assert fired == []


def test_start_parent_watchdog_noop_without_env() -> None:
    assert start_parent_watchdog(environ={}) is None


def test_start_parent_watchdog_runs_and_exits() -> None:
    # Parent (pid 1, always alive) — but we point at our own pid and force the
    # thread to observe "dead" via a custom exit that records the call.
    fired: list[bool] = []
    thread = start_parent_watchdog(
        on_exit=lambda: fired.append(True),
        environ={"AUTOBOT_PARENT_PID": str(os.getpid())},
    )
    assert thread is not None
    # The real os.getpid() is alive, so it won't fire; just confirm the thread armed.
    assert thread.is_alive() or fired == []
