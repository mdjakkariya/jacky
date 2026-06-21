"""Application assembly and entry point.

:func:`build` is the composition root: the single place that chooses concrete
implementations and wires them together behind the :mod:`autobot.core.interfaces`
protocols. Everything else depends only on the protocols, so changing a model,
back-end, or policy is a change here and nowhere else.

Phase 1 wires the orchestrator state machine and the permission gate (with the
audit log and sandboxed filesystem tools) in front of the language model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autobot.config import Settings
from autobot.core.events import AmplitudeSink, VisibilitySink
from autobot.core.interfaces import AudioSource, LanguageModel, TextToSpeech
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

if TYPE_CHECKING:
    from autobot.memory.store import MemoryStore


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
    # Reflect the *active* brain so the log is truthful: cloud turns must not be
    # labelled with the local model name (and vice-versa).
    llm_label = (
        f"claude/{settings.anthropic_model}"
        if settings.llm_provider == "anthropic"
        else settings.llm_model
    )
    header = (
        f"model: {llm_label} · stt: {settings.stt_model} · "
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


def _build_llm(
    settings: Settings,
    registry: ToolRegistry,
    transcript: Transcript,
    memory: MemoryStore | None,
) -> LanguageModel:
    """Pick the language-model backend: local Ollama (default) or Anthropic (opt-in).

    Cloud is disclosed and degrades gracefully — a missing key or the missing
    ``cloud`` extra falls back to local rather than failing startup.
    """
    log = get_logger("app")
    if settings.llm_provider == "anthropic":
        try:
            from autobot.llm.anthropic_llm import AnthropicLanguageModel

            llm = AnthropicLanguageModel(settings, registry, transcript, memory=memory)
            log.info("llm provider=anthropic model=%s (OFF-DEVICE)", settings.anthropic_model)
            print(
                f"[llm] CLOUD mode — Claude ({settings.anthropic_model}). Your requests and "
                "remembered profile are sent to Anthropic. Actions still run locally."
            )
            return llm
        except ImportError:
            log.warning("cloud LLM extra missing, falling back to local")
            print(
                "[llm] cloud needs the 'anthropic' package — run `uv sync --extra cloud`. "
                "Using local Ollama for now."
            )
        except ValueError as exc:
            log.warning("cloud LLM unavailable, falling back to local: %s", exc)
            print(f"[llm] cloud unavailable ({exc}) — using local Ollama.")
    return OllamaLanguageModel(settings, registry, transcript, memory=memory)


def build(
    settings: Settings | None = None,
    on_state: StateListener | None = None,
    amplitude_sink: AmplitudeSink | None = None,
    on_visibility: VisibilitySink | None = None,
) -> Orchestrator:
    """Compose a fully wired :class:`Orchestrator`.

    Args:
        settings: Configuration to use; defaults to :meth:`Settings.load`.
        on_state: Optional state-transition listener. Defaults to the console
            printer; the daemon passes one that also publishes to its event bus.
        amplitude_sink: Optional callback fed normalized loudness (0..1) while
            capturing speech and while speaking; the daemon passes the bus's
            ``publish_amplitude`` so the orb reacts to real audio.
        on_visibility: Optional show/hide sink for the UI. When given, the
            voice ``dismiss`` tool is registered and wired to hide the orb; the
            daemon passes the bus's ``publish_visibility``.

    Returns:
        A ready-to-run orchestrator. Constructing it loads the STT model, opens
        the audit log, prepares the sandbox, and connects to Ollama.
    """
    settings = settings or Settings.load()
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

    # Phase 4: persistent personalization. The store is read into the prompt each
    # turn and grown via the (gated) memory tools.
    memory = None
    if settings.allow_memory:
        from autobot.memory.store import MemoryStore
        from autobot.tools.memory import register_memory_tools

        memory = MemoryStore(settings.memory_db)
        register_memory_tools(registry, memory)
        log.info("memory ENABLED db=%s", settings.memory_db)
        name = memory.get_name()
        print(f"[memory] personalization ON{f' — hi {name}!' if name else ''}")
    if settings.allow_web:
        # The one tool that reaches off-device; only registered when opted in.
        from autobot.tools.web import register_web_tools

        register_web_tools(registry, settings)
        from autobot.secrets import get_secret

        using_api = settings.web_provider != "ddgs" and bool(get_secret("web_api_key"))
        provider = "API" if using_api else "ddgs scraping"
        log.info("web search ENABLED provider=%s (queries leave the device)", provider)
        print(f"[web] web search ENABLED via {provider} — queries leave the device.")

    if on_visibility is not None:
        # A UI is attached: let the user dismiss the orb by voice ("go away").
        from autobot.tools.orb import register_orb_tools

        visibility = on_visibility
        register_orb_tools(registry, lambda: visibility(False))
        log.info("orb dismiss tool ENABLED")

    # Permission gate: audit everything, confirm destructive actions only.
    audit = AuditLog(settings.audit_db)
    gate = PermissionGate(registry, audit, TerminalConfirmer())

    # Per-session transcript (readable conversation + debug notes).
    transcript = _build_transcript(settings)

    # Reloadable LLM: rebuilt from fresh settings + Keychain when the Settings
    # view changes the provider/model/key — no restart needed (applies next turn).
    from autobot.llm.reloadable import ReloadableLanguageModel

    llm = ReloadableLanguageModel(lambda: _build_llm(Settings.load(), registry, transcript, memory))

    # Reloadable STT: rebuilt (new model loaded) when the Settings view changes
    # the speech model — no restart needed (applies on the next transcription).
    from autobot.stt.reloadable import ReloadableSTT

    stt = ReloadableSTT(lambda: FasterWhisperSTT(Settings.load()))

    return Orchestrator(
        settings=settings,
        audio=_build_audio_source(settings, amplitude_sink),
        stt=stt,
        llm=llm,
        gate=gate,
        wake_gate=_build_wake_gate(settings),
        tts=_build_tts(settings, amplitude_sink),
        transcript=transcript,
        on_state=on_state or _print_transition,
        memory=memory,
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
