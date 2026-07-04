from __future__ import annotations

import pytest

from autobot.agent.harness import AgentHarness
from autobot.agent.providers.openai_compatible import OpenAICompatibleModel
from autobot.app import _build_llm
from autobot.config import Settings
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry


def test_build_llm_openai_returns_harness_wrapping_openai_model() -> None:
    # This path builds a real OpenAI client (no injected fake), so it needs the optional
    # `cloud` extra; skip where it isn't installed (e.g. CI, which syncs only dev+daemon).
    pytest.importorskip("openai")
    settings = Settings(
        llm_provider="openai",
        openai_base_url="https://openrouter.ai/api/v1",
        llm_model="openai/gpt-4o-mini",
    )
    llm = _build_llm(settings, ToolRegistry(), NullTranscript(), None)
    assert isinstance(llm, AgentHarness)
    assert isinstance(llm._model, OpenAICompatibleModel)
