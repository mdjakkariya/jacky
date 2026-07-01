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


def ensure_syscap(bundled_dir: str | None) -> str | None:
    """Locate the bundled ``autobot-syscap`` binary, seeding it on first run.

    The orb app passes ``AUTOBOT_SYSCAP_DIR`` pointing at its bundled binaries
    (Tauri extracts the target-triple-suffixed sidecar there). Returns the path to
    a runnable binary, or ``None`` if it isn't available (dev runs degrade to
    mic-only far-end capture).

    Args:
        bundled_dir: Directory containing the bundled ``autobot-syscap`` binary,
            or ``None`` (e.g. a source run without the Tauri bundle).

    Returns:
        Absolute path to a runnable ``autobot-syscap`` binary, or ``None``.
    """
    target = Path("~/.autobot/bin/autobot-syscap").expanduser()
    if target.exists():
        return str(target)
    if not bundled_dir:
        return None
    src = Path(bundled_dir).expanduser() / "autobot-syscap"
    if not src.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    target.chmod(0o755)
    _log.info("seeded autobot-syscap from bundle -> %s", target)
    return str(target)
