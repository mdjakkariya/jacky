from __future__ import annotations

from typing import Any

import pytest

import autobot.cli as cli


def test_send_chat_posts_and_returns_reply() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True, "reply": "done"}

    reply = cli.send_chat("http://127.0.0.1:8766", "fix the bug", post=fake_post)
    assert reply == "done"
    assert seen["url"].endswith("/chat")
    assert seen["payload"] == {"text": "fix the bug"}


def test_send_chat_surfaces_error_reply() -> None:
    def fake_post(url, payload, timeout):  # type: ignore[no-untyped-def]
        return {"ok": False, "reply": "", "error": "chat unavailable"}

    reply = cli.send_chat("http://x", "hi", post=fake_post)
    assert "unavailable" in reply.lower() or "couldn" in reply.lower()


def test_send_chat_handles_non_json_response() -> None:
    # A stranger answering on the port returns non-JSON → json.loads raises ValueError.
    # send_chat must return a friendly string, never crash with a traceback.
    def fake_post(url, payload, timeout):  # type: ignore[no-untyped-def]
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    reply = cli.send_chat("http://x", "hi", post=fake_post)
    assert isinstance(reply, str) and reply
    assert "couldn't read" in reply.lower() or "response" in reply.lower()


def test_send_chat_handles_connection_error() -> None:
    def fake_post(url, payload, timeout):  # type: ignore[no-untyped-def]
        raise OSError("Connection refused")

    reply = cli.send_chat("http://x", "hi", post=fake_post)
    assert isinstance(reply, str)
    assert "couldn't reach" in reply.lower()


def test_daemon_up_probe() -> None:
    # is_daemon_up returns True when the readiness probe succeeds, False on connection error.
    assert cli.is_daemon_up("http://x", probe=lambda url, timeout: True) is True

    def raising_probe(url: str, timeout: float) -> bool:
        raise OSError

    assert cli.is_daemon_up("http://x", probe=raising_probe) is False


def test_main_one_shot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen_port: list[int] = []
    monkeypatch.setattr(cli, "ensure_daemon", lambda base, port: seen_port.append(port))
    monkeypatch.setattr(cli, "send_chat", lambda base, text, **k: "the reply")
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
