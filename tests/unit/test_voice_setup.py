"""Tests for on-demand voice-model provisioning (status + download orchestration).

The network fetchers are injected as fakes, so the progress maths and skip logic are
exercised without touching the network.
"""

from __future__ import annotations

from pathlib import Path

from autobot.config import Settings
from autobot.voice_setup import (
    FractionFn,
    ModelSpec,
    _dir_bytes,
    _download_with_progress,
    _hf_snapshot_complete,
    download_missing,
    needed_keys,
    status,
)


def _spec(
    key: str, present: bool, weight: float = 1.0, calls: list[str] | None = None
) -> ModelSpec:
    def fetch(on_fraction: FractionFn) -> None:
        if calls is not None:
            calls.append(key)
        on_fraction(0.5)
        on_fraction(1.0)

    return ModelSpec(key=key, label=key, present=lambda: present, fetch=fetch, weight=weight)


def test_needed_keys_includes_wake_only_for_ml_detector() -> None:
    assert needed_keys(Settings()) == ["voice", "stt"]  # default detector is transcript-match
    assert "wake" in needed_keys(Settings(wake_detector="openwakeword"))


def test_status_ready_when_needed_models_present() -> None:
    specs = [_spec("voice", True), _spec("stt", True), _spec("wake", False)]
    st = status(Settings(), specs)
    assert st["ready"] is True  # wake not needed for the default detector
    assert st["models"] == {"voice": True, "stt": True, "wake": False}


def test_status_not_ready_when_stt_missing() -> None:
    specs = [_spec("voice", True), _spec("stt", False), _spec("wake", True)]
    assert status(Settings(), specs)["ready"] is False


def test_download_missing_skips_present_and_reports_monotonic_progress() -> None:
    calls: list[str] = []
    specs = [
        _spec("voice", True, calls=calls),  # already present -> skipped
        _spec("stt", False, weight=4.0, calls=calls),
        _spec("wake", False, calls=calls),
    ]
    seen: list[tuple[float, str]] = []
    download_missing(Settings(), lambda f, s: seen.append((f, s)), specs)

    assert calls == ["stt", "wake"]  # the present voice model was not re-fetched
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions)  # progress never goes backwards
    assert all(0.0 <= f <= 1.0 for f in fractions)
    assert seen[-1] == (1.0, "Ready")


def test_download_missing_all_present_is_immediately_ready() -> None:
    seen: list[tuple[float, str]] = []
    specs = [_spec("voice", True), _spec("stt", True)]
    download_missing(Settings(), lambda f, s: seen.append((f, s)), specs)
    assert seen == [(1.0, "Ready")]


def _make_hf_repo(cache: Path, *, complete: bool) -> Path:
    """Build a fake HF cache repo dir for faster-whisper-small.en."""
    repo = cache / "models--Systran--faster-whisper-small.en"
    blobs = repo / "blobs"
    rev = repo / "snapshots" / "abc123"
    blobs.mkdir(parents=True)
    rev.mkdir(parents=True)
    if complete:
        weights = rev / "model.bin"
        weights.write_bytes(b"\0" * (11 * 1024 * 1024))  # > _MIN_STT_BYTES
    else:
        # Mid-download: an .incomplete blob and no resolved weights yet.
        (blobs / "deadbeef.incomplete").write_bytes(b"\0" * (5 * 1024 * 1024))
    return repo


def test_hf_snapshot_complete_detects_finished_vs_in_progress(tmp_path: Path) -> None:
    assert _hf_snapshot_complete(_make_hf_repo(tmp_path / "done", complete=True)) is True
    # A directory that exists mid-download must NOT count as complete (the old bug).
    assert _hf_snapshot_complete(_make_hf_repo(tmp_path / "wip", complete=False)) is False


def test_dir_bytes_counts_real_files_and_filters_by_match(tmp_path: Path) -> None:
    (tmp_path / "models--a--faster-whisper-small.en").mkdir(parents=True)
    (tmp_path / "models--a--faster-whisper-small.en" / "f.bin").write_bytes(b"x" * 100)
    (tmp_path / "models--b--other").mkdir(parents=True)
    (tmp_path / "models--b--other" / "g.bin").write_bytes(b"y" * 999)
    only_small = _dir_bytes(tmp_path, match=lambda p: "small.en" in p.name)
    assert only_small == 100  # the unrelated repo is excluded


def test_download_with_progress_reports_growth_and_caps_below_one() -> None:
    import threading
    import time

    fractions: list[float] = []
    state = {"bytes": 0}
    release = threading.Event()

    def run() -> None:
        for step in (250, 500, 1000):  # the "download" growing on disk
            state["bytes"] = step
            time.sleep(0.01)
        release.wait(1.0)  # hold until the test lets it finish

    t = threading.Thread(
        target=lambda: _download_with_progress(
            run=run,
            bytes_so_far=lambda: state["bytes"],
            approx_total=1000,
            on_fraction=fractions.append,
            poll_s=0.005,
        )
    )
    t.start()
    time.sleep(0.08)
    release.set()
    t.join(timeout=2.0)

    assert fractions, "expected at least one progress sample"
    assert max(fractions) <= 0.99  # never reports done until run() actually returns
    assert fractions == sorted(fractions)  # monotonic


def test_download_with_progress_reraises_errors() -> None:
    import pytest

    def boom() -> None:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        _download_with_progress(
            run=boom, bytes_so_far=lambda: 0, approx_total=100, on_fraction=lambda _f: None
        )
