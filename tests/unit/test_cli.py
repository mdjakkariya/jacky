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
