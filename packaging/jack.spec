# PyInstaller spec: freeze the `jack` coding CLI into one self-contained binary with
# no system-Python dependency. Contains both roles — the CLI client and (via
# `jack serve`) the headless daemon. LEAN: the coder never imports the bulk of the
# voice stack (faster-whisper / ctranslate2 / sounddevice / onnxruntime / piper), so
# those are excluded to keep the binary small. numpy stays IN despite being part of
# that stack — see the comment below on why it can't be excluded.
#
# Build:  make freeze-cli   (== pyinstaller packaging/jack.spec)
# Output: dist/jack          (dist/jack.exe on Windows)
#
# Heavy deps are lazy-imported, so PyInstaller's static analysis misses them — list
# the coder stack via collect_all / collect_submodules below. Expect to iterate this
# against the first build's runtime errors on each OS (missing module / missing dylib).
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

_ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # repo root (spec lives in packaging/)
_ENTRY = os.path.join(_ROOT, "packaging", "jack_entry.py")

datas: list = []
binaries: list = []
hiddenimports: list = ["anthropic", "openai", "ollama", "fastapi", "keyring"]

# numpy is NOT part of the voice stack we can lean out of: autobot.core.types (the
# Risk/State/ToolCall value objects, imported by every profile) and
# autobot.orchestrator.state_machine (the turn loop `daemon serve` always wires up,
# coder profile included) import it unconditionally at module level for their type
# aliases and real array ops (see the ModuleNotFoundError this raised on first build).
# So it has to be bundled even in the lean coder freeze — everything else genuinely
# voice-only (faster_whisper/ctranslate2/onnxruntime/sounddevice/piper/...) stays excluded.
for _pkg in (
    "keyring",
    "pydantic",
    "pydantic_core",
    "rich",
    "prompt_toolkit",
    "numpy",
):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:  # optional/not installed in this env — skip, iterate per OS
        pass

# These load submodules dynamically, so static analysis misses them.
for _mod in ("uvicorn", "anthropic", "openai", "fastapi", "starlette"):
    hiddenimports += collect_submodules(_mod)

a = Analysis(
    [_ENTRY],
    pathex=[os.path.join(_ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # The coder never loads the rest of the voice stack; exclude it so the binary
    # stays lean. numpy is deliberately NOT here — see the comment above.
    excludes=[
        "faster_whisper", "ctranslate2", "onnxruntime", "sounddevice",
        "piper", "av", "tokenizers", "huggingface_hub", "torch", "torchaudio",
        "tkinter", "matplotlib", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jack",
    console=True,
    onefile=True,
    target_arch=None,  # build native for the host runner (the CI matrix covers arches)
)
