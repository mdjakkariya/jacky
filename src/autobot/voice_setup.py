"""On-demand provisioning of the voice models, so the app ships small.

Voice is **opt-in**. To keep the bundle compact the app no longer ships the speech
models; the first time the user enables voice we download what the current config
needs and report progress so the Settings view can show a bar:

* the Piper **voice** (TTS) — from the Piper voices repo,
* the Whisper **STT** model — via faster-whisper's HuggingFace download,
* the openWakeWord **wake** model — only used by the ML wake detector.

Privacy: these fetch *public model files only* — no audio, text, or user data ever
leaves the machine (same hosts the engine already used when it downloaded models
silently). The network calls live behind small injectable fetchers so the
orchestration and progress maths are unit-tested without touching the network.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from autobot.logging_setup import get_logger

if TYPE_CHECKING:
    from autobot.config import Settings

_log = get_logger("voice")

# (overall 0..1, human-readable stage) — what the UI renders as a progress bar.
ProgressFn = Callable[[float, str], None]
# Per-model fraction 0..1, reported by a fetcher as it downloads.
FractionFn = Callable[[float], None]

# Piper voice files (the .onnx model + its .json config) live under this prefix.
_PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high"


@dataclass(frozen=True)
class ModelSpec:
    """One downloadable model: how to tell if it's here, and how to fetch it.

    ``weight`` is a rough relative size used only to apportion the overall progress
    bar (so the big STT model doesn't make the bar jump from 0 to 100 at the end).
    """

    key: str  # stable id: "voice" | "stt" | "wake"
    label: str  # human label for the progress line
    present: Callable[[], bool]
    fetch: Callable[[FractionFn], None]
    weight: float = 1.0


def _voice_present(settings: Settings) -> bool:
    """True when the Piper voice model (and its config) are on disk."""
    target = Path(settings.tts_voice).expanduser()
    return target.exists() and target.with_name(target.name + ".json").exists()


def _stt_present(settings: Settings) -> bool:
    """Best-effort check that the configured STT model is already downloaded.

    The two engines cache differently: whisper.cpp keeps a ``ggml-<model>.bin`` under
    pywhispercpp's data dir, while faster-whisper caches a ``…faster-whisper-<model>``
    directory in the HuggingFace cache. We look for the file/dir rather than importing
    the heavy runtime just to check.
    """
    model = settings.stt_model
    if settings.stt_engine == "whisper_cpp":
        name = f"ggml-{model}.bin"
        candidates = [
            Path("~/Library/Application Support/pywhispercpp/models").expanduser(),
            Path("~/.local/share/pywhispercpp/models").expanduser(),
        ]
        return any((d / name).exists() for d in candidates)
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:  # hub not importable — assume not present so we offer to fetch
        return False
    needle = f"faster-whisper-{model}".lower()
    cache = Path(HF_HUB_CACHE)
    if not cache.exists():
        return False
    return any(needle in p.name.lower() for p in cache.glob("models--*"))


def _wake_present(settings: Settings) -> bool:
    """Best-effort check for the openWakeWord model cache (ML detector only)."""
    cache = Path("~/.cache/openwakeword").expanduser()
    return cache.exists() and any(cache.iterdir())


def model_specs(settings: Settings) -> list[ModelSpec]:
    """The full set of voice models, in download order (voice, STT, wake)."""
    return [
        ModelSpec(
            "voice", "voice", lambda: _voice_present(settings), _make_voice_fetch(settings), 1.0
        ),
        ModelSpec(
            "stt",
            "speech recognition",
            lambda: _stt_present(settings),
            _make_stt_fetch(settings),
            4.0,
        ),
        ModelSpec(
            "wake", "wake word", lambda: _wake_present(settings), _make_wake_fetch(settings), 1.0
        ),
    ]


def needed_keys(settings: Settings) -> list[str]:
    """Models required for voice to actually work under the *current* config.

    Talking needs the voice (TTS) and the STT model; the wake model is only used by
    the ML wake detector, so it's required only when that detector is selected.
    """
    keys = ["voice", "stt"]
    if settings.wake_detector == "openwakeword":
        keys.append("wake")
    return keys


def status(settings: Settings, specs: Sequence[ModelSpec] | None = None) -> dict[str, object]:
    """Which voice models are present, and whether voice is ready to enable."""
    specs = specs or model_specs(settings)
    present = {s.key: s.present() for s in specs}
    needed = needed_keys(settings)
    ready = all(present.get(k, False) for k in needed)
    return {"models": present, "needed": needed, "ready": ready}


def download_missing(
    settings: Settings,
    on_progress: ProgressFn,
    specs: Sequence[ModelSpec] | None = None,
) -> None:
    """Download whichever voice models are missing, reporting weighted progress.

    Already-present models are skipped. ``on_progress`` is called with the overall
    fraction (0..1) and a stage label; it ends on ``(1.0, "Ready")``.
    """
    specs = specs or model_specs(settings)
    todo = [s for s in specs if not s.present()]
    if not todo:
        on_progress(1.0, "Ready")
        return
    total = sum(s.weight for s in todo) or 1.0
    done = 0.0
    for spec in todo:
        base, label = done, f"Downloading {spec.label}…"
        on_progress(base / total, label)

        def fraction(
            f: float, base: float = base, weight: float = spec.weight, label: str = label
        ) -> None:
            clamped = 0.0 if f < 0.0 else 1.0 if f > 1.0 else f
            on_progress((base + weight * clamped) / total, label)

        _log.info("downloading voice model key=%s", spec.key)
        spec.fetch(fraction)
        done += spec.weight
    on_progress(1.0, "Ready")


# --- real fetchers (network; not exercised in unit tests) ---------------------


def _http_download(url: str, dest: Path, on_fraction: FractionFn) -> None:
    """Stream ``url`` to ``dest`` (atomic), reporting fraction from Content-Length."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as resp:  # fixed HTTPS model host
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        with tmp.open("wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                read += len(chunk)
                if total:
                    on_fraction(read / total)
    tmp.replace(dest)
    on_fraction(1.0)


def _make_voice_fetch(settings: Settings) -> Callable[[FractionFn], None]:
    def fetch(on_fraction: FractionFn) -> None:
        target = Path(settings.tts_voice).expanduser()
        _http_download(f"{_PIPER_BASE}/{target.name}", target, on_fraction)
        # The small .json config: fetch quietly (no separate progress weight).
        _http_download(
            f"{_PIPER_BASE}/{target.name}.json",
            target.with_name(target.name + ".json"),
            lambda _f: None,
        )

    return fetch


def _make_stt_fetch(settings: Settings) -> Callable[[FractionFn], None]:
    def fetch(on_fraction: FractionFn) -> None:
        # The model pulls into its cache on first construction/download. We can't get
        # fine-grained byte progress without reaching into the libraries' internals,
        # so report a coarse start→finish around the blocking download.
        on_fraction(0.02)
        if settings.stt_engine == "whisper_cpp":
            from pywhispercpp.model import Model

            Model(settings.stt_model, print_realtime=False, print_progress=False)
        else:
            from faster_whisper.utils import download_model

            download_model(settings.stt_model)
        on_fraction(1.0)

    return fetch


def _make_wake_fetch(settings: Settings) -> Callable[[FractionFn], None]:
    def fetch(on_fraction: FractionFn) -> None:
        on_fraction(0.02)
        import openwakeword.utils

        openwakeword.utils.download_models([settings.wake_model])
        on_fraction(1.0)

    return fetch
