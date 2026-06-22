# PyInstaller spec: freeze the engine daemon into a single self-contained binary
# (`autobot-daemon`) with no dependency on a system Python. The Tauri orb app ships
# this binary as a "sidecar" and launches it, so users install one .dmg.
#
# Build:  make freeze         (== pyinstaller packaging/autobot-daemon.spec)
# Output: dist/autobot-daemon (a single executable)
#
# NOTE: the engine imports heavy runtimes lazily (inside functions), so PyInstaller's
# static analysis can't see them — they MUST be listed below via collect_all /
# hiddenimports. Expect to iterate this list against the first build's errors on a
# real Mac (missing module / missing dylib). Keep it to the "try-it" default stack:
# cloud LLM (anthropic) or ollama, faster-whisper STT, Piper TTS, sounddevice mic.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

_ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # repo root (spec lives in packaging/)
_ENTRY = os.path.join(_ROOT, "src", "autobot", "daemon", "__main__.py")

datas: list = []
binaries: list = []
hiddenimports: list = ["numpy", "anthropic", "ollama", "fastapi"]

# Packages that carry native libs and/or data files PyInstaller won't find on its own.
for _pkg in (
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "sounddevice",
    "piper",
    "silero_vad",
    "av",
    "tokenizers",
    "huggingface_hub",
    "pydantic",
    "pydantic_core",
):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:  # optional/not installed in this env — skip, iterate on Mac
        pass

# These load submodules dynamically, so static analysis misses them.
for _mod in ("uvicorn", "anthropic", "fastapi", "starlette"):
    hiddenimports += collect_submodules(_mod)

a = Analysis(
    [_ENTRY],
    pathex=[os.path.join(_ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest"],  # keep the bundle lean
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="autobot-daemon",
    console=True,  # the daemon logs to stdout; the orb captures it
    onefile=True,
    target_arch="arm64",  # Apple Silicon; set None for universal2 if you build fat
)
