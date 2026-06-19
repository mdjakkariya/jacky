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
from autobot.core.interfaces import AudioSource, TextToSpeech
from autobot.io.audio import PushToTalkRecorder
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.logging_setup import get_logger, setup_logging
from autobot.orchestrator.state_machine import Orchestrator
from autobot.orchestrator.wake_gate import PassThroughGate, SttWakeGate, WakeGate
from autobot.stt.faster_whisper_stt import FasterWhisperSTT
from autobot.tools.audit import AuditLog
from autobot.tools.builtin import register_builtins
from autobot.tools.filesystem import register_filesystem_tools
from autobot.tools.permission import PermissionGate, TerminalConfirmer
from autobot.tools.registry import ToolRegistry
from autobot.tools.sandbox import Sandbox


def _build_audio_source(settings: Settings) -> AudioSource:
    """Pick the input recorder for the configured mode and wake detector.

    The hands-free path needs the optional ``wake`` dependencies (silero-vad, and
    openWakeWord only for that detector); if missing we fail with a clear hint.
    """
    if settings.input_mode == "ptt":
        return PushToTalkRecorder(settings)

    from autobot.io.listening import MicFrameSource, VadRecorder, WakeWordVadRecorder
    from autobot.io.wake_vad import OpenWakeWord, SileroVad

    source = MicFrameSource(settings)
    try:
        vad = SileroVad()  # imports the heavy runtime
        if settings.wake_detector == "openwakeword":
            return WakeWordVadRecorder(
                settings=settings, source=source, wake=OpenWakeWord(settings.wake_model), vad=vad
            )
        # Default: transcribe-then-match — VAD captures each phrase, the wake word
        # is matched on the transcript by the wake gate.
        return VadRecorder(settings, source, vad)
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise SystemExit(
            "Hands-free mode needs the 'wake' extra: run `uv sync --extra wake` "
            "(or set AUTOBOT_INPUT=ptt for push-to-talk)."
        ) from exc


def _build_wake_gate(settings: Settings) -> WakeGate:
    """Text-level wake gate: STT match for the 'stt' detector, else pass-through."""
    if settings.input_mode != "ptt" and settings.wake_detector == "stt":
        return SttWakeGate(settings.wake_phrase, settings.follow_up_window_s)
    return PassThroughGate()


def _build_tts(settings: Settings) -> TextToSpeech:
    """Build the voice output: Piper if enabled and available, else silent.

    Falls back to a no-op so a missing 'tts' extra or voice model degrades to a
    text-only assistant rather than crashing.
    """
    from autobot.tts.null_tts import NullTTS

    log = get_logger("tts")
    if not settings.tts_enabled:
        print("[tts] voice output OFF (AUTOBOT_TTS=0) — text only.")
        return NullTTS()
    try:
        from autobot.tts.piper_tts import PiperTTS

        tts = PiperTTS(settings)
        log.info("voice output ready voice=%s", settings.tts_voice)
        print(f"[tts] voice output READY (voice: {settings.tts_voice})")
        return tts
    except (ImportError, FileNotFoundError) as exc:
        log.warning("voice output disabled: %s", exc)
        print(f"[tts] voice output OFF — {exc}")
        print("      Fix: `uv sync --extra tts` and download a voice (see README).")
        return NullTTS()


def build(settings: Settings | None = None) -> Orchestrator:
    """Compose a fully wired :class:`Orchestrator`.

    Args:
        settings: Configuration to use; defaults to :meth:`Settings.from_env`.

    Returns:
        A ready-to-run orchestrator. Constructing it loads the STT model, opens
        the audit log, prepares the sandbox, and connects to Ollama.
    """
    settings = settings or Settings.from_env()
    log_path = setup_logging(settings)
    log = get_logger("app")
    log.info(
        "starting input=%s llm=%s stt=%s sandbox=%s",
        settings.input_mode,
        settings.llm_model,
        settings.stt_model,
        settings.sandbox_dir,
    )
    print(f"[log] writing debug log to {log_path}")

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
        wake_gate=_build_wake_gate(settings),
        tts=_build_tts(settings),
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
