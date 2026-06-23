"""Application assembly and entry point.

:func:`build` is the composition root: the single place that chooses concrete
implementations and wires them together behind the :mod:`autobot.core.interfaces`
protocols. Everything else depends only on the protocols, so changing a model,
back-end, or policy is a change here and nowhere else.

Phase 1 wires the orchestrator state machine and the permission gate (with the
audit log and sandboxed filesystem tools) in front of the language model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from autobot.config import Settings
from autobot.core.events import AmplitudeSink, VisibilitySink
from autobot.core.interfaces import AudioSource, LanguageModel, SpeechToText, TextToSpeech
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
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry
from autobot.tools.sandbox import Sandbox

if TYPE_CHECKING:
    from autobot.io.listening import FrameSource
    from autobot.memory.store import MemoryStore


def _build_mic_source(settings: Settings) -> FrameSource:
    """The microphone frame source: echo-cancelled (AEC) if enabled and available.

    AEC needs the macOS Voice-Processing path; any failure (wrong platform, missing
    pyobjc, runtime error) falls back to the plain mic, so barge-in just won't
    engage rather than the app breaking.
    """
    from autobot.io.listening import MicFrameSource

    if settings.aec:
        try:
            from autobot.io.aec_mac import VoiceProcessingMicSource

            src = VoiceProcessingMicSource(settings)
            get_logger("app").info("mic input: Voice-Processing (AEC on) — barge-in safe")
            print("[mic] echo cancellation ON (Voice-Processing) — barge-in enabled.")
            return src
        except Exception as exc:  # any failure -> plain mic, barge-in stays off
            get_logger("app").warning("AEC unavailable, using plain mic: %s", exc)
            print(f"[mic] AEC unavailable ({exc}) — plain mic; barge-in disabled.")
    return MicFrameSource(settings)


def _build_audio_source(
    settings: Settings,
    on_level: AmplitudeSink | None = None,
    on_voice: Callable[[bool], None] | None = None,
    source: FrameSource | None = None,
) -> AudioSource:
    """Pick the input recorder for the configured mode and wake detector.

    ``source`` lets the caller pass a pre-built mic source (so the same AEC engine
    used for capture is also used to play TTS); if omitted, one is built here.

    The hands-free path needs the optional ``wake`` dependency (onnxruntime, plus
    openWakeWord only for that detector); if missing we fail with a clear hint.
    """
    if settings.input_mode == "ptt":
        return PushToTalkRecorder(settings)

    from autobot.io.listening import VadRecorder, WakeWordVadRecorder
    from autobot.io.wake_vad import OpenWakeWord, SileroVad

    source = source or _build_mic_source(settings)
    try:
        vad = SileroVad()  # onnxruntime; loads the vendored silero model
        if settings.wake_detector == "openwakeword":
            return WakeWordVadRecorder(
                settings=settings,
                source=source,
                wake=OpenWakeWord(settings.wake_model),
                vad=vad,
                on_level=on_level,
                reload=Settings.load,  # live endpointing tunables (no restart)
                on_voice=on_voice,
            )
        # Default: transcribe-then-match — VAD captures each phrase, the wake word
        # is matched on the transcript by the wake gate.
        return VadRecorder(
            settings, source, vad, on_level=on_level, reload=Settings.load, on_voice=on_voice
        )
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
    # Bound the sessions folder: drop the oldest beyond session_keep on startup.
    from autobot.session_log import prune_sessions

    pruned = prune_sessions(settings.session_dir, settings.session_keep)
    if pruned:
        get_logger("app").info("pruned old session files n=%d", len(pruned))
    try:
        transcript = FileTranscript(settings.session_dir, header)
    except OSError as exc:
        # A transcript write/dir failure must never take down the engine — log it
        # and run without a session file.
        get_logger("app").warning("session transcript disabled (%s): %s", settings.session_dir, exc)
        return NullTranscript()
    get_logger("app").info("session transcript file=%s", transcript.path)
    print(f"[session] transcript: {transcript.path}")
    return transcript


def _build_tts(
    settings: Settings,
    on_level: AmplitudeSink | None = None,
    player: object | None = None,
) -> TextToSpeech:
    """Build the voice output: Piper if enabled and available, else silent.

    ``player`` (an AudioPlayer) is where speech is rendered. On the AEC path it's the
    Voice-Processing engine's output node, so macOS cancels Jack's voice from the mic
    and barge-in is safe; otherwise it defaults to the plain system output.

    Falls back to a no-op so a missing 'tts' extra or voice model degrades to a
    text-only assistant rather than crashing.
    """
    from autobot.tts.null_tts import NullTTS

    log = get_logger("tts")
    if not settings.tts_enabled:
        print("[tts] voice output OFF (AUTOBOT_TTS=0) — text only.")
        return NullTTS()
    try:
        from autobot.tts.piper_tts import AudioPlayer, PiperTTS

        tts = PiperTTS(
            settings, on_level=on_level, player=player if isinstance(player, AudioPlayer) else None
        )
        routed = " (through AEC engine)" if isinstance(player, AudioPlayer) else ""
        log.info("voice output ready voice=%s%s", settings.tts_voice, routed)
        print(f"[tts] voice output READY (voice: {settings.tts_voice}){routed}")
        return tts
    except (ImportError, FileNotFoundError) as exc:
        log.warning("voice output disabled: %s", exc)
        print(f"[tts] voice output OFF — {exc}")
        print("      Fix: `uv sync --extra tts` and download a voice (see README).")
        return NullTTS()


def _build_stt(settings: Settings) -> SpeechToText:
    """Pick the speech engine: faster-whisper (CPU, default) or whisper.cpp (Metal).

    whisper.cpp degrades gracefully — a missing ``whispercpp`` extra falls back to
    faster-whisper rather than failing startup.
    """
    log = get_logger("app")
    if settings.stt_engine == "whisper_cpp":
        try:
            from autobot.stt.whisper_cpp_stt import WhisperCppSTT

            stt = WhisperCppSTT(settings)
            log.info("stt engine=whisper.cpp model=%s (Metal/GPU)", settings.stt_model)
            return stt
        except ImportError:
            log.warning("whisper.cpp extra missing, falling back to faster-whisper")
            print(
                "[stt] whisper.cpp needs the 'whispercpp' extra — run "
                "`uv sync --extra whispercpp`. Using faster-whisper for now."
            )
    return FasterWhisperSTT(settings)


def _build_confirmer(
    settings: Settings,
    tts: TextToSpeech,
    audio: AudioSource,
    stt: SpeechToText,
    on_confirm: Callable[[str, bool], None] | None,
    on_confirm_clear: Callable[[], None] | None,
    poll_click: Callable[[], bool | None] | None,
) -> object:
    """Pick how destructive actions are confirmed: by voice (hands-free) or terminal.

    Hands-free (the mic supports bounded capture) gets the spoken yes/no flow with a
    card on the orb; push-to-talk / no-VAD falls back to a terminal ``[y/N]`` prompt.
    """
    rec_cont = getattr(audio, "record_continuation", None)
    if not callable(rec_cont):
        from autobot.tools.permission import TerminalConfirmer

        return TerminalConfirmer()

    from autobot.tools.confirm import VoiceConfirmer

    def listen(timeout_s: float) -> str:
        clip = rec_cont(timeout_s)
        if clip is None or clip.size == 0:
            return ""
        return stt.transcribe(clip).text

    flush = getattr(audio, "flush", None)

    # In chat mode confirm by the card click only (no speaking / mic). Read live so a
    # runtime voice⇄chat switch is honoured.
    def is_chat() -> bool:
        return Settings.load().interaction_mode == "chat"

    # Tag the broadcast card with the mode so the voice orb ignores chat-mode
    # confirmations (otherwise it pops up a duplicate card over the chat drawer).
    show = None
    if on_confirm is not None:

        def show(prompt: str) -> None:
            on_confirm(prompt, is_chat())

    return VoiceConfirmer(
        speak=tts.speak,
        listen=listen,
        on_show=show,
        on_clear=on_confirm_clear,
        poll_click=poll_click,
        flush=flush if callable(flush) else None,
        is_chat=is_chat,
        timeout_s=settings.confirm_timeout_s,
    )


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
    on_voice: Callable[[bool], None] | None = None,
    on_confirm: Callable[[str, bool], None] | None = None,
    on_confirm_clear: Callable[[], None] | None = None,
    poll_click: Callable[[], bool | None] | None = None,
    on_context: Callable[[dict[str, object]], None] | None = None,
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
        on_voice: Optional voice-activity sink (``True`` while the user is
            speaking, ``False`` when they stop), so the orb shows a "listening"
            animation only during real speech; the daemon wires it to the bus.
        on_confirm: Optional sink (prompt -> None) shown when a destructive action
            needs confirmation; the daemon wires it to the bus so the orb shows a
            card. When given (and the mic supports it), confirmations are by voice.
        on_confirm_clear: Optional sink invoked when a confirmation resolves, so the
            orb hides the card.
        poll_click: Optional source returning a clicked Yes/No (``True``/``False``)
            for a pending confirmation, or ``None``; lets a card click answer
            alongside voice. The daemon wires it to the confirmation inbox.
        on_context: Optional sink fed the per-turn context-usage payload (used,
            window, model, cache stats), so the chat meter can render it; the daemon
            wires it to the bus's ``publish_context``.

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

    # Empty-the-Trash: a destructive action, so the gate confirms it (by voice).
    from autobot.tools.trash import register_trash_tools

    register_trash_tools(registry)

    # Per-session transcript (readable conversation + debug notes).
    transcript = _build_transcript(settings)

    # Seed the bundled default voice on a fresh install (so TTS works out of the
    # box); the orb app passes AUTOBOT_VOICE_DIR pointing at its bundled voices.
    import os

    from autobot.tts.voices import ensure_voice

    ensure_voice(settings.tts_voice, os.environ.get("AUTOBOT_VOICE_DIR"))

    # Voice I/O is built before the gate so the confirmer can speak the prompt and
    # listen for the spoken yes/no. Build the mic source first: if it's the AEC
    # engine, route TTS through it (its play()) so macOS cancels Jack's own voice
    # from the mic — that's what makes full-duplex barge-in safe on speakers. If AEC
    # isn't available it's a plain mic with no play(), so TTS uses the system output
    # and we run half-duplex.
    mic = _build_mic_source(settings)
    aec_player = mic if getattr(mic, "aec_active", False) and hasattr(mic, "play") else None
    tts = _build_tts(settings, amplitude_sink, player=aec_player)
    audio = _build_audio_source(settings, amplitude_sink, on_voice, source=mic)

    # Reloadable STT: rebuilt (new model loaded) when the Settings view changes
    # the speech model — no restart needed (applies on the next transcription).
    from autobot.stt.reloadable import ReloadableSTT

    stt = ReloadableSTT(lambda: _build_stt(Settings.load()))

    # Permission gate: audit everything, confirm destructive actions only. The
    # confirmer asks by voice (with a card on the orb) when hands-free.
    audit = AuditLog(settings.audit_db)
    confirmer = _build_confirmer(
        settings, tts, audio, stt, on_confirm, on_confirm_clear, poll_click
    )
    # Permission-aware: refuse a tool whose macOS permission is missing (and open the
    # right Settings pane) instead of letting it fail deep in AppleScript.
    from autobot import permissions

    gate = PermissionGate(
        registry,
        audit,
        confirmer,  # type: ignore[arg-type]
        permission_status=permissions.status_of,
        on_permission_needed=permissions.open_pane,
    )

    # Reloadable LLM: rebuilt from fresh settings + Keychain when the Settings
    # view changes the provider/model/key — no restart needed (applies next turn).
    from autobot.llm.reloadable import ReloadableLanguageModel

    llm = ReloadableLanguageModel(lambda: _build_llm(Settings.load(), registry, transcript, memory))

    # Make barge-in readiness obvious at startup: it engages only when the user
    # wants it AND the mic is echo-cancelled (so Jack can't interrupt itself).
    aec_on = bool(getattr(audio, "aec_active", False))
    if settings.barge_in and aec_on:
        log.info("barge-in READY (aec_active=True) — talk over Jack to interrupt")
        print("[barge-in] READY — you can talk over Jack to interrupt it.")
    elif settings.barge_in:
        log.warning("barge-in requested but INACTIVE: aec_active=False (AEC off/failed)")
        print(
            "[barge-in] INACTIVE — echo cancellation isn't active, so barge-in is off "
            "(enable AEC + restart; check the [mic] line above)."
        )

    return Orchestrator(
        settings=settings,
        audio=audio,
        stt=stt,
        llm=llm,
        gate=gate,
        wake_gate=_build_wake_gate(settings),
        tts=tts,
        transcript=transcript,
        on_state=on_state or _print_transition,
        memory=memory,
        on_context=on_context,
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
