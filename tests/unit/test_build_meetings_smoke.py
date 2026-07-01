"""Smoke test: build() registers meeting tools when allow_meetings=True.

No Ollama connection, no mic, no sidecar are needed — the voice I/O and LLM are
built lazily or monkeypatched, so the test is fast and fully offline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from autobot.config import Settings


def test_build_registers_meeting_tools_when_enabled(monkeypatch: Any, tmp_path: Any) -> None:
    """Meeting tool names appear in the registry when allow_meetings=True."""
    # Monkeypatch the LLM builder so build() needs no running Ollama instance.
    fake_llm = MagicMock()
    fake_llm.complete.return_value = "summary"
    fake_llm.run_turn.return_value = iter([])

    import autobot.app as _app

    monkeypatch.setattr(_app, "_build_llm", lambda *a, **kw: fake_llm)

    s = Settings(
        allow_meetings=True,
        meetings_dir=str(tmp_path),
        interaction_mode="chat",
        # Disable capability flags that pull in heavy optional dependencies.
        allow_app_control=False,
        allow_system_info=False,
        allow_system_toggles=False,
        allow_file_search=False,
        allow_clipboard=False,
        allow_reminders=False,
        allow_notes=False,
        allow_memory=False,
        allow_file_io=False,
        allow_web=False,
        allow_mcp=False,
        # No TTS, session log off to avoid filesystem side-effects.
        tts_enabled=False,
        session_log=False,
    )

    orch = _app.build(settings=s)

    # The meeting tools live in the registry behind the permission gate.
    names = {spec.name for spec in orch._gate._registry.specs()}
    assert {"start_meeting", "stop_meeting", "meeting_status"} <= names, (
        f"Expected meeting tools in registry. Found: {names!r}"
    )
