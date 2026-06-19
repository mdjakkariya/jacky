"""Application assembly and the Phase 0 interaction loop.

:func:`build` is the composition root: it is the single place that chooses
concrete implementations and wires them together behind the
:mod:`autobot.core.interfaces` protocols. Everything else depends only on the
protocols, so changing a model or back-end is a change here and nowhere else.

In later phases this loop is replaced by the orchestrator state machine and the
engine moves behind a daemon API, but the composition-root pattern stays.
"""

from __future__ import annotations

from dataclasses import dataclass

from autobot.config import Settings
from autobot.core.interfaces import AudioSource, LanguageModel, SpeechToText
from autobot.io.audio import PushToTalkRecorder
from autobot.llm.ollama_llm import OllamaLanguageModel
from autobot.stt.faster_whisper_stt import FasterWhisperSTT
from autobot.tools.registry import default_registry


@dataclass(slots=True)
class Application:
    """The wired assistant: an audio source, an STT engine, and a language model."""

    settings: Settings
    audio: AudioSource
    stt: SpeechToText
    llm: LanguageModel

    def run_once(self) -> None:
        """Record one utterance, transcribe it, and print the assistant's reply."""
        audio = self.audio.record_clip()
        transcription = self.stt.transcribe(audio)
        if transcription.is_empty:
            print("[stt] (heard nothing — try again)")
            return
        print(f"[you] {transcription.text}   (confidence {transcription.confidence:.2f})")
        reply = self.llm.run_turn(transcription.text)
        print(f"[autobot] {reply}\n")

    def run(self) -> None:
        """Run the push-to-talk loop until interrupted with Ctrl-C."""
        print("=" * 60)
        print(" Autobot — Phase 0 (push-to-talk)")
        print(f" STT: {self.settings.stt_model}   LLM: {self.settings.llm_model}")
        print(' Try: "what time is it"   |   Ctrl-C to quit')
        print("=" * 60)
        try:
            while True:
                self.run_once()
        except KeyboardInterrupt:
            print("\nBye.")


def build(settings: Settings | None = None) -> Application:
    """Compose a fully wired :class:`Application`.

    Args:
        settings: Configuration to use; defaults to :meth:`Settings.from_env`.

    Returns:
        A ready-to-run :class:`Application`. Constructing it loads the STT model
        and connects to Ollama.
    """
    settings = settings or Settings.from_env()
    registry = default_registry()
    return Application(
        settings=settings,
        audio=PushToTalkRecorder(settings),
        stt=FasterWhisperSTT(settings),
        llm=OllamaLanguageModel(settings, registry),
    )


def main() -> None:
    """Console-script / ``python -m autobot`` entry point."""
    build().run()


if __name__ == "__main__":
    main()
