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


def test_coder_prompt_handles_unactionable_input_with_a_capability_hint() -> None:
    # On unclear/unactionable input, the coder should give a concise capability hint, not a
    # bare "what do you mean?" (the U7 UX principle) — kept general, no incident-specific text.
    low = system_prompt("chat", coder=True).lower()
    assert "vague" in low or "unclear" in low
    assert "help with" in low


def test_coder_prompt_has_continuation_principle() -> None:
    # The coder must carry a task to completion in one go rather than narrate a next step
    # and stop (the observed mid-task stall) — kept as a general principle, no incident text.
    low = system_prompt("chat", coder=True).lower()
    assert "step" in low
    assert "without stopping" in low or "do not stop" in low or "don't stop" in low


def test_coder_prompt_forbids_sleep_polling_long_commands() -> None:
    # The observed "stuck" pattern was running a long test suite behind a sleep/tail poll,
    # which blocks with no output. The prompt must steer to running it directly.
    low = system_prompt("chat", coder=True).lower()
    assert "sleep" in low and "poll" in low


def test_coder_prompt_requires_verify_and_honest_reporting() -> None:
    # Verify before claiming done, and never manufacture a green result.
    low = system_prompt("chat", coder=True).lower()
    assert "verify" in low
    assert "honest" in low or "never say tests pass" in low


def test_coder_prompt_establishes_the_jack_identity() -> None:
    # The assistant is "Jack" (the product name), not the underlying model. With no name in the
    # coder prompt, the cloud model introduced ITSELF ("Hey! I'm Claude, your coding agent") —
    # so the coder prompt must name the assistant, mirroring the voice/chat SYSTEM_PROMPT.
    assert "Jack" in system_prompt("chat", coder=True)
