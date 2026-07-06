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
    assert calls[1][1] == {"value": "approve", "text": ""}


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


def test_main_ctrl_c_sends_best_effort_reject(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ctrl-C best-effort unblocks a worker parked awaiting a reply by POSTing a reject.
    def interrupted(base: str, port: int) -> None:
        raise KeyboardInterrupt

    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        calls.append((url, payload))
        return {"status": "done"}

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    monkeypatch.setattr(cli, "_post", fake_post)
    rc = cli.main(["--port", "9001", "do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()
    assert calls == [("http://127.0.0.1:9001/coder/reply", {"value": "reject"})]


def test_main_ctrl_c_swallows_post_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The best-effort reject must never change the exit path if it fails.
    def interrupted(base: str, port: int) -> None:
        raise KeyboardInterrupt

    def boom_post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "ensure_daemon", interrupted)
    monkeypatch.setattr(cli, "_post", boom_post)
    rc = cli.main(["do a thing"])
    assert rc == 130
    assert "cancel" in capsys.readouterr().err.lower()


def test_start_turn_posts_text() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen["url"] = url
        seen["payload"] = payload
        return {"status": "plan", "reply": "1. x", "todo": ["x"]}

    resp = cli.start_turn("http://x", "do it", post=fake_post)
    assert isinstance(resp, dict)
    assert resp["status"] == "plan"
    assert seen["url"].endswith("/coder/turn") and seen["payload"] == {"text": "do it"}


def test_answer_posts_value_and_text() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen["url"] = url
        seen["payload"] = payload
        return {"status": "done", "reply": "ok"}

    cli.answer("http://x", "refine", "use bash", post=fake_post)
    assert seen["url"].endswith("/coder/reply")
    assert seen["payload"] == {"value": "refine", "text": "use bash"}


def test_start_turn_maps_connection_error_to_string() -> None:
    def boom(url, payload, timeout):  # type: ignore[no-untyped-def]
        raise OSError("refused")

    assert "couldn't reach" in cli.start_turn("http://x", "hi", post=boom).lower()  # type: ignore[union-attr]


def test_main_no_args_launches_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port: None)
    import autobot.cli.tui as tui

    monkeypatch.setattr(tui, "run", lambda base_url, cwd: launched.append((base_url, cwd)))
    rc = cli.main(["--port", "8766"])
    assert rc == 0 and launched and launched[0][0].endswith("8766")


def test_main_no_args_without_textual_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port: None)

    def raise_import(base_url: str, cwd: str) -> None:
        raise ImportError("No module named 'textual'")

    import autobot.cli.tui as tui

    monkeypatch.setattr(tui, "run", raise_import)
    rc = cli.main([])
    assert rc == 1 and "tui" in capsys.readouterr().err.lower()


def test_main_with_text_still_one_shot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port: None)
    monkeypatch.setattr(cli, "run_coder_turn", lambda base, text, **k: "the reply")
    rc = cli.main(["do a thing"])
    assert rc == 0 and "the reply" in capsys.readouterr().out
