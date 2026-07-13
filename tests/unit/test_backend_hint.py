"""First-run hint appears only when no usable LLM backend is configured."""

from __future__ import annotations

from autobot.update import backend_hint


def test_no_hint_when_cloud_key_present() -> None:
    assert backend_hint("anthropic", has_cloud_key=True, ollama_reachable=False) is None


def test_no_hint_when_ollama_reachable() -> None:
    assert backend_hint("ollama", has_cloud_key=False, ollama_reachable=True) is None


def test_hint_when_cloud_selected_but_no_key() -> None:
    hint = backend_hint("anthropic", has_cloud_key=False, ollama_reachable=False)
    assert hint is not None
    assert "jack config" in hint and "ollama" in hint.lower()


def test_hint_when_nothing_configured() -> None:
    assert backend_hint("ollama", has_cloud_key=False, ollama_reachable=False) is not None
