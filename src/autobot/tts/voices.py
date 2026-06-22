"""Seed a usable Piper voice on first run from the one bundled in the app.

A fresh install has no voice in ``~/.autobot/voices``, so TTS would be silent. The
orb app bundles a default voice and tells the engine where (``AUTOBOT_VOICE_DIR``);
on startup we copy it into place if the configured voice is missing. A Piper voice
is a pair: ``<name>.onnx`` and its ``<name>.onnx.json`` config — both are copied.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from autobot.logging_setup import get_logger

_log = get_logger("tts")


def _config_path(onnx: Path) -> Path:
    """The Piper config sitting next to a voice: ``X.onnx`` -> ``X.onnx.json``."""
    return onnx.with_name(onnx.name + ".json")


def copy_voice(src: Path, target: Path) -> bool:
    """Copy a Piper voice (``.onnx`` + ``.onnx.json``) from ``src`` to ``target``.

    Returns ``True`` if the voice was copied, ``False`` if ``src`` doesn't exist.
    """
    if not src.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    cfg = _config_path(src)
    if cfg.exists():
        shutil.copy2(cfg, _config_path(target))
    return True


def ensure_voice(voice_path: str, bundled_dir: str | None) -> None:
    """Seed the configured voice from ``bundled_dir`` if it isn't present yet.

    No-op when the voice already exists or no bundle dir is given (e.g. a source
    run, where the user supplied their own voice).
    """
    target = Path(voice_path).expanduser()
    if target.exists() or not bundled_dir:
        return
    src = Path(bundled_dir).expanduser() / target.name
    if copy_voice(src, target):
        _log.info("seeded default voice from bundle -> %s", target)
    else:
        _log.warning("no bundled voice at %s; TTS will be silent until one is added", src)
