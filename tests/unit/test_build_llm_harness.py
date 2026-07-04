from __future__ import annotations

from autobot.agent.harness import AgentHarness
from autobot.app import _build_llm
from autobot.config import Settings
from autobot.session_log import NullTranscript
from autobot.tools.registry import ToolRegistry


def test_build_llm_returns_a_harness_for_local() -> None:
    # Local provider: no network, no key. The Ollama client is built lazily on first
    # use, so construction here must not touch it — _build_llm returns a harness.
    llm = _build_llm(Settings(llm_provider="ollama"), ToolRegistry(), NullTranscript(), None)
    assert isinstance(llm, AgentHarness)
    assert hasattr(llm, "run_turn")
    assert hasattr(llm, "set_delivery_mode")
