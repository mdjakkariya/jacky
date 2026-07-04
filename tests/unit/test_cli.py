from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import autobot.cli as cli


def test_daemon_up_probe() -> None:
    # is_daemon_up returns True when the readiness probe succeeds, False on connection error.
    assert cli.is_daemon_up("http://x", probe=lambda url, timeout: True) is True

    def raising_probe(url: str, timeout: float) -> bool:
        raise OSError

    assert cli.is_daemon_up("http://x", probe=raising_probe) is False


_Post = Callable[[str, dict[str, Any], float], dict[str, Any]]
_Call = tuple[str, dict[str, Any]]


def _scripted_post(script: list[dict[str, Any]]) -> tuple[_Post, list[_Call]]:
    """Return a fake post() that yields the next scripted response each call."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append((url, payload))
        return script[len(calls) - 1]

    return post, calls


def test_run_coder_turn_plan_approve_done() -> None:
    post, calls = _scripted_post(
        [
            {"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]},
            {"status": "done", "reply": "Edited foo."},
        ]
    )
    reply = cli.run_coder_turn(
        "http://x", "edit foo", post=post, prompt=lambda r: {"value": "approve"}
    )
    assert reply == "Edited foo."
    assert calls[0][0].endswith("/coder/turn")
    assert calls[1][0].endswith("/coder/reply")
    assert calls[1][1] == {"value": "approve"}


def test_run_coder_turn_pending_command_yes() -> None:
    post, _ = _scripted_post(
        [
            {"status": "plan", "reply": "1. run tests", "todo": ["run tests"]},
            {"status": "pending", "kind": "command", "prompt": "Run `pytest -q`?"},
            {"status": "done", "reply": "Tests passed."},
        ]
    )
    answers = iter([{"value": "approve"}, {"value": "yes"}])
    reply = cli.run_coder_turn("http://x", "run tests", post=post, prompt=lambda r: next(answers))
    assert reply == "Tests passed."


def test_run_coder_turn_reject() -> None:
    post, _ = _scripted_post(
        [
            {"status": "plan", "reply": "1. edit foo", "todo": ["edit foo"]},
            {"status": "done", "reply": "Okay, I won't make any changes."},
        ]
    )
    reply = cli.run_coder_turn("http://x", "edit", post=post, prompt=lambda r: {"value": "reject"})
    assert "won't" in reply.lower()


def test_run_coder_turn_handles_connection_error() -> None:
    def post(url, payload, timeout):  # type: ignore[no-untyped-def]
        raise OSError("Connection refused")

    reply = cli.run_coder_turn("http://x", "hi", post=post, prompt=lambda r: {"value": "approve"})
    assert "couldn't reach" in reply.lower()


def test_main_one_shot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen_port: list[int] = []
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port: seen_port.append(port))
    monkeypatch.setattr(cli, "run_coder_turn", lambda base, text, **k: "the reply")
    rc = cli.main(["--port", "9001", "do a thing"])
    assert rc == 0
    assert "the reply" in capsys.readouterr().out
    assert seen_port == [9001]  # main() forwards --port to the daemon spawn


def test_main_returns_1_when_daemon_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(base: str, port: int) -> None:
        raise TimeoutError("coder daemon did not start")

    monkeypatch.setattr(cli, "ensure_daemon", boom)
    rc = cli.main(["do a thing"])
    assert rc == 1
    assert "did not start" in capsys.readouterr().err


def test_main_surfaces_daemon_startup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # When the spawned daemon dies (e.g. missing extra), ensure_daemon raises RuntimeError
    # with the reason — main() must print it and exit 1, not hang or dump a traceback.
    def boom(base: str, port: int) -> None:
        raise RuntimeError("the coder daemon couldn't start (exit 1). ... needs the daemon extra")

    monkeypatch.setattr(cli, "ensure_daemon", boom)
    rc = cli.main(["do a thing"])
    assert rc == 1
    assert "daemon extra" in capsys.readouterr().err


def test_main_handles_ctrl_c_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ctrl-C during startup/turn must exit cleanly (130), not raise KeyboardInterrupt.
    def interrupted(base: str, port: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    rc = cli.main(["do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()
