"""The composition root wires a tool selector into the local LLM."""

from __future__ import annotations

import pytest

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


def test_build_llm_anthropic_off_falls_back_to_local_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the fallback branch deterministically: patch get_secret so it always
    # returns None regardless of whether a real Keychain entry exists.  This causes
    # _require_key() (anthropic_llm.py line 810-816) to raise ValueError, which
    # _build_llm catches (app.py line 298-300) and converts to an OllamaLanguageModel.
    # The strong assertion verifies the fallback contract — not merely "no crash".
    monkeypatch.setattr("autobot.secrets.get_secret", lambda *_args, **_kwargs: None)

    model = _build_llm(
        Settings(context_tokens=4096, llm_provider="anthropic"),
        ToolRegistry(),
        NullTranscript(),
        None,
    )
    assert isinstance(model, OllamaLanguageModel)
