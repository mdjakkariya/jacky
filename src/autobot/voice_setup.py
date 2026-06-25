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

# A finished STT weights file dwarfs this; anything smaller is a stub/partial download.
_MIN_STT_BYTES = 10_000_000  # 10 MB

# Rough on-disk sizes (bytes) of the CTranslate2 faster-whisper builds. Used ONLY to
# render a real progress bar by polling bytes-on-disk against this total — HF's
# downloader doesn't hand us byte counts — so an approximate figure is fine.
_STT_APPROX_BYTES: dict[str, int] = {
    "tiny.en": 75_000_000,
    "tiny": 75_000_000,
    "base.en": 145_000_000,
    "base": 145_000_000,
    "small.en": 484_000_000,
    "small": 484_000_000,
    "medium.en": 1_530_000_000,
    "medium": 1_530_000_000,
    "large-v1": 3_090_000_000,
    "large-v2": 3_090_000_000,
    "large-v3": 3_090_000_000,
    "distil-small.en": 166_000_000,
    "distil-medium.en": 789_000_000,
    "distil-large-v2": 1_510_000_000,
    "distil-large-v3": 1_510_000_000,
}

# Same, for whisper.cpp's single ggml weights file (Metal path).
_GGML_APPROX_BYTES: dict[str, int] = {
    "tiny.en": 78_000_000,
    "tiny": 78_000_000,
    "base.en": 148_000_000,
    "base": 148_000_000,
    "small.en": 488_000_000,
    "small": 488_000_000,
    "medium.en": 1_530_000_000,
    "medium": 1_530_000_000,
    "large-v1": 3_100_000_000,
    "large-v2": 3_100_000_000,
    "large-v3": 3_100_000_000,
}


def _whispercpp_dirs() -> list[Path]:
    """Candidate directories where pywhispercpp caches its ggml model files."""
    return [
        Path("~/Library/Application Support/pywhispercpp/models").expanduser(),
        Path("~/.local/share/pywhispercpp/models").expanduser(),
    ]


def _dir_bytes(root: Path, match: Callable[[Path], bool] | None = None) -> int:
    """Total bytes of real files under ``root`` (skipping symlinks to avoid double-count).

    With ``match`` we only sum HuggingFace repo dirs (``models--*``) whose name matches —
    so we measure just the model being downloaded. Partial ``.incomplete`` blobs count,
    which is what makes the size grow smoothly during a download.
    """
    if not root.exists():
        return 0
    bases = [p for p in root.glob("models--*") if match(p)] if match is not None else [root]
    total = 0
    for base in bases:
        for f in base.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:  # a file vanished mid-scan (download churn) — ignore
                continue
    return total


def _hf_snapshot_complete(repo_dir: Path) -> bool:
    """True only when an HF model snapshot has *finished* downloading.

    A download in flight leaves ``*.incomplete`` blobs and no resolved ``model.bin`` in
    the snapshot, so the directory existing is NOT enough to call it ready — the bug this
    guards against (status said "ready" the instant the dir was created).
    """
    blobs = repo_dir / "blobs"
    if blobs.exists() and any(blobs.glob("*.incomplete")):
        return False  # a download is still in flight
    snaps = repo_dir / "snapshots"
    if not snaps.exists():
        return False
    for rev in snaps.iterdir():
        if not rev.is_dir():
            continue
        weights = rev / "model.bin"
        try:
            if weights.exists() and weights.stat().st_size > _MIN_STT_BYTES:
                return True
        except OSError:
            continue
    return False


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
        return any(
            (d / name).exists() and (d / name).stat().st_size > _MIN_STT_BYTES
            for d in _whispercpp_dirs()
        )
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:  # hub not importable — assume not present so we offer to fetch
        return False
    cache = Path(HF_HUB_CACHE)
    if not cache.exists():
        return False
    needle = f"faster-whisper-{model}".lower()
    # Require a *complete* snapshot, not just the dir: HF creates the directory the
    # instant a download starts, so a name match alone would report "ready" mid-download
    # and let the user into voice mode before STT can actually transcribe.
    return any(
        _hf_snapshot_complete(p) for p in cache.glob("models--*") if needle in p.name.lower()
    )


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


def _stt_disk_bytes(settings: Settings) -> int:
    """Bytes of the configured STT model currently on disk (incl. partial downloads)."""
    model = settings.stt_model
    if settings.stt_engine == "whisper_cpp":
        name = f"ggml-{model}.bin"
        total = 0
        for d in _whispercpp_dirs():
            # pywhispercpp downloads to the final name; some versions use a temp first.
            for cand in (d / name, d / (name + ".tmp"), d / (name + ".part")):
                try:
                    if cand.exists():
                        total += cand.stat().st_size
                except OSError:
                    continue
        return total
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:
        return 0
    needle = f"faster-whisper-{model}".lower()
    return _dir_bytes(Path(HF_HUB_CACHE), match=lambda p: needle in p.name.lower())


def _download_with_progress(
    run: Callable[[], object],
    bytes_so_far: Callable[[], int],
    approx_total: int,
    on_fraction: FractionFn,
    poll_s: float = 0.5,
) -> None:
    """Run a blocking model download on a thread, reporting progress from disk size.

    faster-whisper/HF and pywhispercpp don't hand a byte-progress callback to us, so we
    watch the destination's growing size against ``approx_total``. The reported fraction
    is capped just below 1.0 until ``run`` actually returns, so the bar only ever hits
    100% when the download is genuinely complete (which is what the readiness check then
    confirms). Any download error is re-raised in the caller's thread.
    """
    import threading

    error: dict[str, Exception] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            run()
        except Exception as exc:  # surface it to the caller below
            error["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=worker, name="model-download", daemon=True)
    thread.start()
    while not done.wait(poll_s):
        if approx_total > 0:
            # Report true on-disk completion: for a resumed download this correctly
            # starts partway, not from zero.
            on_fraction(min(0.99, bytes_so_far() / approx_total))
    thread.join()
    if "exc" in error:
        raise error["exc"]


def _make_stt_fetch(settings: Settings) -> Callable[[FractionFn], None]:
    def fetch(on_fraction: FractionFn) -> None:
        on_fraction(0.0)
        model = settings.stt_model
        if settings.stt_engine == "whisper_cpp":
            from pywhispercpp.model import Model

            _download_with_progress(
                run=lambda: Model(model, print_realtime=False, print_progress=False),
                bytes_so_far=lambda: _stt_disk_bytes(settings),
                approx_total=_GGML_APPROX_BYTES.get(model, 0),
                on_fraction=on_fraction,
            )
        else:
            from faster_whisper.utils import download_model

            _download_with_progress(
                run=lambda: download_model(model),
                bytes_so_far=lambda: _stt_disk_bytes(settings),
                approx_total=_STT_APPROX_BYTES.get(model, 0),
                on_fraction=on_fraction,
            )
        on_fraction(1.0)

    return fetch


def _make_wake_fetch(settings: Settings) -> Callable[[FractionFn], None]:
    def fetch(on_fraction: FractionFn) -> None:
        on_fraction(0.02)
        import openwakeword.utils

        openwakeword.utils.download_models([settings.wake_model])
        on_fraction(1.0)

    return fetch
