"""A language-model proxy that can rebuild itself when settings change.

The engine builds its LLM once at startup. To let the Settings view change the
provider / model / API key **without a restart**, the orchestrator's LLM is this
proxy: it delegates to an inner model, and when the daemon marks it dirty (on a
settings or secret change) it rebuilds the inner model from fresh settings + the
Keychain on the next turn. If the rebuild fails, it keeps the working model.

Thread-safe: ``mark_dirty`` is called from the daemon thread; ``run_turn`` runs on
the engine thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from autobot.core.interfaces import LanguageModel
from autobot.core.types import ToolExecutor
from autobot.logging_setup import get_logger

_log = get_logger("llm")

LLMFactory = Callable[[], LanguageModel]


class ReloadableLanguageModel:
    """Wraps a built :class:`LanguageModel`, rebuilding it on demand."""

    def __init__(self, factory: LLMFactory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._dirty = False
        self._inner: LanguageModel = factory()  # build eagerly (fail fast at startup)

    def mark_dirty(self) -> None:
        """Request a rebuild before the next turn (called when settings change)."""
        with self._lock:
            self._dirty = True
        _log.info("llm marked for reload")

    def run_turn(self, user_text: str, execute: ToolExecutor) -> str:
        """Rebuild from fresh settings if dirty, then handle the turn."""
        with self._lock:
            if self._dirty:
                try:
                    self._inner = self._factory()
                    _log.info("llm reloaded from updated settings")
                except Exception as exc:  # keep the working model on failure
                    _log.warning("llm reload failed, keeping current: %s", exc)
                self._dirty = False
            inner = self._inner
        return inner.run_turn(user_text, execute)

    def context_usage(self) -> dict[str, Any] | None:
        """Delegate the context-meter usage to the active inner model (if it has it)."""
        fn = getattr(self._inner, "context_usage", None)
        return fn() if callable(fn) else None
