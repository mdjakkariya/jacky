"""Client helpers for the ``jack config`` surface (GET /settings, POST /secret)."""

from __future__ import annotations

from typing import Any

from autobot.cli.client import get_settings, post_secret


def test_get_settings_uses_injected_getter() -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> Any:
        calls.append(url)
        return {"llm_provider": "anthropic", "_secrets": {"anthropic_api_key": True}}

    data = get_settings("http://x", get=fake_get)
    assert data["llm_provider"] == "anthropic"
    assert calls == ["http://x/settings"]


def test_post_secret_shapes_payload() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen.update(url=url, payload=payload)
        return {"ok": True}

    res = post_secret("http://x", "anthropic_api_key", "sk-1", post=fake_post)
    assert res == {"ok": True}
    assert seen["url"] == "http://x/secret"
    assert seen["payload"] == {"name": "anthropic_api_key", "value": "sk-1"}
