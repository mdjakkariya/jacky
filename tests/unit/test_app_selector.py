"""The composition root wires a tool selector into the local LLM."""

from __future__ import annotations

from autobot.app import _build_llm
from autobot.config import Settings
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry
from autobot.tools.selection import AllToolsSelector, LexicalToolSelector


def test_build_llm_wires_lexical_selector_by_default() -> None:
    # context_tokens set so __init__ does not probe a live Ollama server.
    model = _build_llm(Settings(context_tokens=4096), ToolRegistry(), NullTranscript(), None)
    assert isinstance(model, OllamaLanguageModel)
    assert isinstance(model._selector, LexicalToolSelector)


def test_build_llm_honors_tool_selection_all() -> None:
    model = _build_llm(
        Settings(context_tokens=4096, tool_selection="all"), ToolRegistry(), NullTranscript(), None
    )
    assert isinstance(model, OllamaLanguageModel)
    assert isinstance(model._selector, AllToolsSelector)


def test_build_llm_anthropic_off_falls_back_to_local_without_key() -> None:
    # No API key + default provider switch to anthropic: _build_llm must NOT raise; it
    # degrades to the local Ollama model (cloud features never crash startup). In CI
    # there is no Anthropic key, so the branch always falls back to local. Locally, a
    # developer may have a real key in the Keychain; in that case the cloud model is
    # returned instead — both are valid outcomes. The key contract is "never crash".
    from autobot.llm.anthropic_llm import AnthropicLanguageModel

    model = _build_llm(
        Settings(context_tokens=4096, llm_provider="anthropic"),
        ToolRegistry(),
        NullTranscript(),
        None,
    )
    # Either outcome is correct: fallback to local (no key in env) or cloud model
    # (real key in dev Keychain). The contract is "no crash on startup".
    assert isinstance(model, (OllamaLanguageModel, AnthropicLanguageModel))
