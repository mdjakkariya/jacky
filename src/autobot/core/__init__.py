"""Core domain types and the interfaces that decouple the pipeline stages.

Importing this subpackage is cheap: it pulls in no model runtimes, so it is
safe to use from tests and from light-weight tooling.
"""

from __future__ import annotations

from autobot.core.interfaces import (
    AudioSource,
    LanguageModel,
    SpeechToText,
    TextToSpeech,
)
from autobot.core.types import (
    AudioClip,
    AuditEntry,
    Decision,
    Risk,
    State,
    ToolCall,
    ToolExecutor,
    ToolResult,
    Transcription,
)

__all__ = [
    "AudioClip",
    "AudioSource",
    "AuditEntry",
    "Decision",
    "LanguageModel",
    "Risk",
    "SpeechToText",
    "State",
    "TextToSpeech",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
    "Transcription",
]
