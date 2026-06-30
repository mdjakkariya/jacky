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

# We run silero's ONNX model directly (no torch); ship the vendored model file so
# importlib.resources can find it inside the frozen bundle.
datas += [(os.path.join(_ROOT, "src", "autobot", "io", "silero_vad.onnx"), "autobot/io")]

# Build-embedded OAuth client secrets (gitignored; absent in open-source/CI builds).
# When present, ship it at the bundle root so client_secrets._candidate_paths() finds it
# via sys._MEIPASS. Absent → no secret bundled (users supply their own app).
_oauth_secrets = os.path.join(_ROOT, "secrets", "oauth_clients.json")
if os.path.isfile(_oauth_secrets):
    datas += [(_oauth_secrets, ".")]

# Packages that carry native libs and/or data files PyInstaller won't find on its own.
for _pkg in (
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "sounddevice",
    "piper",
    "av",
    "tokenizers",
    "huggingface_hub",
    "pydantic",
    "pydantic_core",
    # MCP client SDK (lazy-imported by autobot.mcp; absent in open-source/CI builds).
    # jsonschema validates tool schemas and loads its meta-schemas from
    # jsonschema_specifications' bundled .json resources — a classic PyInstaller miss,
    # so collect those data files explicitly.
    "mcp",
    "jsonschema",
    "jsonschema_specifications",
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

# PyObjC frameworks used for AEC + the permission status checks. They're imported
# lazily inside functions, so make sure the freeze includes them (else the
# Permissions tab can't read Accessibility/Automation status and shows "Unknown").
for _fw in ("AVFoundation", "ApplicationServices", "CoreServices", "Foundation"):
    hiddenimports.append(_fw)

a = Analysis(
    [_ENTRY],
    pathex=[os.path.join(_ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Keep the bundle lean. torch/torchaudio are no longer dependencies (we run
    # silero's ONNX directly); exclude them so a stale env can't bloat the binary.
    excludes=["tkinter", "matplotlib", "pytest", "torch", "torchaudio"],
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
