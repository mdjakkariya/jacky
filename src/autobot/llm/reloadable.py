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

    def _ensure(self) -> LanguageModel:
        """Return the (possibly freshly rebuilt) inner model, holding the lock.

        Rebuilds from the factory when dirty; keeps the current model if the rebuild
        fails so a misconfigured key or transient error never kills the session.
        """
        with self._lock:
            if self._dirty:
                try:
                    self._inner = self._factory()
                    _log.info("llm reloaded from updated settings")
                except Exception as exc:  # keep the working model on failure
                    _log.warning("llm reload failed, keeping current: %s", exc)
                self._dirty = False
            return self._inner

    def run_turn(
        self,
        user_text: str,
        execute: ToolExecutor,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Rebuild from fresh settings if dirty, then handle the turn.

        Forwards the optional ``on_event`` streaming callback to the inner model — the
        concrete ``AgentHarness`` accepts it; it is intentionally not on the minimal
        ``LanguageModel`` protocol (so 2-arg fakes/callers still satisfy it), hence the
        ``call-arg`` ignore on the streaming branch.
        """
        inner = self._ensure()
        if on_event is None:
            return inner.run_turn(user_text, execute)
        return inner.run_turn(user_text, execute, on_event=on_event)  # type: ignore[call-arg]

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """Forward to the (lazily built) inner model; same reload semantics as run_turn."""
        return self._ensure().complete(prompt, temperature=temperature)

    def context_usage(self) -> dict[str, Any] | None:
        """Delegate the context-meter usage to the active inner model (if it has it)."""
        fn = getattr(self._inner, "context_usage", None)
        return fn() if callable(fn) else None

    def new_session(self) -> None:
        """Reset the active inner model's conversation (the chat's "New chat")."""
        with self._lock:
            inner = self._inner
        fn = getattr(inner, "new_session", None)
        if callable(fn):
            fn()

    def set_delivery_mode(self, mode: str) -> None:
        """Tell the active inner model how the reply is delivered ('chat'/'voice')."""
        with self._lock:
            inner = self._inner
        fn = getattr(inner, "set_delivery_mode", None)
        if callable(fn):
            fn(mode)

    def resume(self, session_id: str) -> bool:
        """Delegate to the active inner model's ``resume`` (if it has one)."""
        with self._lock:
            inner = self._inner
        fn = getattr(inner, "resume", None)
        return bool(fn(session_id)) if callable(fn) else False

    def list_sessions(self) -> list[dict[str, Any]]:
        """Delegate to the active inner model's ``list_sessions`` (if it has one)."""
        with self._lock:
            inner = self._inner
        fn = getattr(inner, "list_sessions", None)
        result = fn() if callable(fn) else []
        return result if isinstance(result, list) else []
