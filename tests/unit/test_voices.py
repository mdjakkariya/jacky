"""Tests for first-run voice seeding (autobot.tts.voices)."""

from __future__ import annotations

from pathlib import Path

from autobot.tts.voices import copy_voice, ensure_voice


def _make_voice(dir_: Path, name: str = "en_US-ryan-high.onnx") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    onnx = dir_ / name
    onnx.write_bytes(b"model")
    onnx.with_name(onnx.name + ".json").write_text("{}")
    return onnx


def test_copy_voice_copies_model_and_config(tmp_path: Path) -> None:
    src = _make_voice(tmp_path / "bundle")
    target = tmp_path / "voices" / "en_US-ryan-high.onnx"

    assert copy_voice(src, target) is True
    assert target.read_bytes() == b"model"
    assert target.with_name(target.name + ".json").read_text() == "{}"


def test_copy_voice_missing_source_returns_false(tmp_path: Path) -> None:
    src = tmp_path / "bundle" / "nope.onnx"
    target = tmp_path / "voices" / "nope.onnx"

    assert copy_voice(src, target) is False
    assert not target.exists()


def test_ensure_voice_seeds_when_missing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _make_voice(bundle)
    target = tmp_path / "voices" / "en_US-ryan-high.onnx"

    ensure_voice(str(target), str(bundle))

    assert target.exists()
    assert target.with_name(target.name + ".json").exists()


def test_ensure_voice_noop_when_present(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _make_voice(bundle)
    target = tmp_path / "voices" / "en_US-ryan-high.onnx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    ensure_voice(str(target), str(bundle))

    assert target.read_bytes() == b"existing"  # untouched


def test_ensure_voice_noop_without_bundle_dir(tmp_path: Path) -> None:
    target = tmp_path / "voices" / "en_US-ryan-high.onnx"

    ensure_voice(str(target), None)

    assert not target.exists()
