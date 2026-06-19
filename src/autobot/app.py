"""Application assembly and entry point.

:func:`build` is the composition root: the single place that chooses concrete
implementations and wires them together behind the :mod:`autobot.core.interfaces`
protocols. Everything else depends only on the protocols, so changing a model,
back-end, or policy is a change here and nowhere else.

Phase 1 wires the orchestrator state machine and the permission gate (with the
audit log and sandboxed filesystem tools) in front of the language model.
"""

from __future__ import annotations

from autobot.config import Settings
from autobot.core.interfaces import AudioSource
from autobot.io.audio import PushToTalkRecorder
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.orchestrator.state_machine import Orchestrator
from autobot.stt.faster_whisper_stt import FasterWhisperSTT
from autobot.tools.audit import AuditLog
from autobot.tools.builtin import register_builtins
from autobot.tools.filesystem import register_filesystem_tools
from autobot.tools.permission import PermissionGate, TerminalConfirmer
from autobot.tools.registry import ToolRegistry
from autobot.tools.sandbox import Sandbox


def _build_audio_source(settings: Settings) -> AudioSource:
    """Pick the input mode: hands-free wake word + VAD, or push-to-talk.

    The hands-free path needs the optional ``wake`` dependencies; if they are
    missing we fail with a clear instruction rather than an opaque ImportError.
    """
    if settings.input_mode == "ptt":
        return PushToTalkRecorder(settings)

    from autobot.io.listening import MicFrameSource, WakeWordVadRecorder
    from autobot.io.wake_vad import OpenWakeWord, SileroVad

    try:
        # The model constructors are what import the optional heavy runtimes.
        wake = OpenWakeWord(settings.wake_model)
        vad = SileroVad()
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise SystemExit(
            "Hands-free mode needs the 'wake' extra: run `uv sync --extra wake` "
            "(or set AUTOBOT_INPUT=ptt for push-to-talk)."
        ) from exc

    return WakeWordVadRecorder(
        settings=settings,
        source=MicFrameSource(settings),
        wake=wake,
        vad=vad,
    )


def build(settings: Settings | None = None) -> Orchestrator:
    """Compose a fully wired :class:`Orchestrator`.

    Args:
        settings: Configuration to use; defaults to :meth:`Settings.from_env`.

    Returns:
        A ready-to-run orchestrator. Constructing it loads the STT model, opens
        the audit log, prepares the sandbox, and connects to Ollama.
    """
    settings = settings or Settings.from_env()

    # Tool catalog: read-only built-ins plus the sandboxed acting tools.
    registry = ToolRegistry()
    register_builtins(registry)
    sandbox = Sandbox(settings.sandbox_dir)
    register_filesystem_tools(registry, sandbox)

    # Permission gate: audit everything, confirm destructive actions only.
    audit = AuditLog(settings.audit_db)
    gate = PermissionGate(registry, audit, TerminalConfirmer())

    return Orchestrator(
        settings=settings,
        audio=_build_audio_source(settings),
        stt=FasterWhisperSTT(settings),
        llm=OllamaLanguageModel(settings, registry),
        gate=gate,
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
