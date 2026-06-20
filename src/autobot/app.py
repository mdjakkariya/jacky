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
from autobot.core.events import AmplitudeSink
from autobot.core.interfaces import AudioSource, TextToSpeech
from autobot.io.audio import PushToTalkRecorder
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.logging_setup import get_logger, setup_logging
from autobot.orchestrator.state_machine import Orchestrator, StateListener, _print_transition
from autobot.orchestrator.wake_gate import PassThroughGate, SttWakeGate, WakeGate
from autobot.session_log import FileTranscript, NullTranscript, Transcript
from autobot.stt.faster_whisper_stt import FasterWhisperSTT
from autobot.tools.audit import AuditLog
from autobot.tools.builtin import register_builtins
from autobot.tools.filesystem import register_filesystem_tools
from autobot.tools.permission import PermissionGate, TerminalConfirmer
from autobot.tools.registry import ToolRegistry
from autobot.tools.sandbox import Sandbox


def _build_audio_source(settings: Settings, on_level: AmplitudeSink | None = None) -> AudioSource:
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
                settings=settings,
                source=source,
                wake=OpenWakeWord(settings.wake_model),
                vad=vad,
                on_level=on_level,
            )
        # Default: transcribe-then-match — VAD captures each phrase, the wake word
        # is matched on the transcript by the wake gate.
        return VadRecorder(settings, source, vad, on_level=on_level)
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


def _build_transcript(settings: Settings) -> Transcript:
    """Open a per-session transcript file (or a no-op if disabled)."""
    if not settings.session_log:
        return NullTranscript()
    header = (
        f"model: {settings.llm_model} · stt: {settings.stt_model} · "
        f"input: {settings.input_mode}/{settings.wake_detector}"
    )
    transcript = FileTranscript(settings.session_dir, header)
    get_logger("app").info("session transcript file=%s", transcript.path)
    print(f"[session] transcript: {transcript.path}")
    return transcript


def _build_tts(settings: Settings, on_level: AmplitudeSink | None = None) -> TextToSpeech:
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

        tts = PiperTTS(settings, on_level=on_level)
        log.info("voice output ready voice=%s", settings.tts_voice)
        print(f"[tts] voice output READY (voice: {settings.tts_voice})")
        return tts
    except (ImportError, FileNotFoundError) as exc:
        log.warning("voice output disabled: %s", exc)
        print(f"[tts] voice output OFF — {exc}")
        print("      Fix: `uv sync --extra tts` and download a voice (see README).")
        return NullTTS()


def build(
    settings: Settings | None = None,
    on_state: StateListener | None = None,
    amplitude_sink: AmplitudeSink | None = None,
) -> Orchestrator:
    """Compose a fully wired :class:`Orchestrator`.

    Args:
        settings: Configuration to use; defaults to :meth:`Settings.from_env`.
        on_state: Optional state-transition listener. Defaults to the console
            printer; the daemon passes one that also publishes to its event bus.
        amplitude_sink: Optional callback fed normalized loudness (0..1) while
            capturing speech and while speaking; the daemon passes the bus's
            ``publish_amplitude`` so the orb reacts to real audio.

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
    if settings.allow_app_control:
        # macOS app lifecycle by voice; gated like everything else (uninstall
        # confirms, the rest are audited WRITEs).
        from autobot.tools.apps import register_app_tools

        register_app_tools(registry)
        log.info("app control ENABLED (open/focus/quit/uninstall …)")
        print("[apps] app control ENABLED — Jack can open/quit apps by voice.")
    if settings.allow_system_info:
        # Read-only system status (battery/wifi/disk) — safe, queries only.
        from autobot.tools.system import register_system_tools

        register_system_tools(registry)
        log.info("system info ENABLED (battery/wifi/disk)")
    if settings.allow_web:
        # The one tool that reaches off-device; only registered when opted in.
        from autobot.tools.web import register_web_tools

        register_web_tools(registry, settings)
        using_api = settings.web_provider != "ddgs" and bool(settings.web_api_key)
        provider = "API" if using_api else "ddgs scraping"
        log.info("web search ENABLED provider=%s (queries leave the device)", provider)
        print(f"[web] web search ENABLED via {provider} — queries leave the device.")

    # Permission gate: audit everything, confirm destructive actions only.
    audit = AuditLog(settings.audit_db)
    gate = PermissionGate(registry, audit, TerminalConfirmer())

    # Per-session transcript (readable conversation + debug notes).
    transcript = _build_transcript(settings)

    return Orchestrator(
        settings=settings,
        audio=_build_audio_source(settings, amplitude_sink),
        stt=FasterWhisperSTT(settings),
        llm=OllamaLanguageModel(settings, registry, transcript),
        gate=gate,
        wake_gate=_build_wake_gate(settings),
        tts=_build_tts(settings, amplitude_sink),
        transcript=transcript,
        on_state=on_state or _print_transition,
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
