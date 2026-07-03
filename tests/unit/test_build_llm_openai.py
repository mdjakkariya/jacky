from __future__ import annotations

from autobot.agent.harness import AgentHarness
from autobot.agent.providers.openai_compatible import OpenAICompatibleModel
from autobot.app import _build_llm
from autobot.config import Settings
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry


def test_build_llm_openai_returns_harness_wrapping_openai_model() -> None:
    settings = Settings(
        llm_provider="openai",
        openai_base_url="https://openrouter.ai/api/v1",
        llm_model="openai/gpt-4o-mini",
    )
    llm = _build_llm(settings, ToolRegistry(), NullTranscript(), None)
    assert isinstance(llm, AgentHarness)
    assert isinstance(llm._model, OpenAICompatibleModel)
