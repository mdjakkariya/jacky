"""Tests for on-demand voice-model provisioning (status + download orchestration).

The network fetchers are injected as fakes, so the progress maths and skip logic are
exercised without touching the network.
"""

from __future__ import annotations

from autobot.config import Settings
from autobot.voice_setup import FractionFn, ModelSpec, download_missing, needed_keys, status


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
