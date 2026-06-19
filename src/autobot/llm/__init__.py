"""Language-model layer: the Ollama-backed orchestrator and its parsing helpers."""

from __future__ import annotations

from autobot.llm.ollama_llm import OllamaLanguageModel, normalize_tool_calls

__all__ = ["OllamaLanguageModel", "normalize_tool_calls"]
