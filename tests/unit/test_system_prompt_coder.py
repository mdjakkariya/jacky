"""The coder system prompt is selected when coder=True."""

from __future__ import annotations

from autobot.llm.ollama_llm import system_prompt


def test_assistant_prompt_by_default() -> None:
    p = system_prompt("chat")
    assert "coding" not in p.lower() or "assistant" in p.lower()


def test_coder_prompt_when_coder_true() -> None:
    p = system_prompt("chat", coder=True)
    assert "code" in p.lower()
    # coder prompt still carries the chat delivery line (reply shown as text)
    assert p != system_prompt("chat", coder=False)


def test_coder_prompt_mentions_the_tools_workflow() -> None:
    p = system_prompt("chat", coder=True)
    low = p.lower()
    assert "read" in low and "edit" in low  # tells the model to read before editing
