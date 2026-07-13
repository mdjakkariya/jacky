# Packaging Jack as a single .dmg (the "wizard")

Goal: a developer downloads **one `.dmg`**, drags Jack to Applications, and a
**first-run wizard** handles the rest — no Python, `uv`, wheels, or manual model
downloads. The orb app *is* the product; the Python engine rides along as an
embedded **Tauri sidecar**.

## Architecture

```
Jack.app
 ├─ the orb UI + chat drawer (Tauri webview: index.html + chat.html)
 └─ autobot-daemon            ← frozen engine binary (PyInstaller), a Tauri sidecar
       the orb spawns it on launch, talks to it over ws://127.0.0.1:8765,
       and kills it on quit
   (no models are bundled — the Piper voice / STT / wake models download on
    demand the first time voice is enabled; see voice_setup.py)
```

Three build steps, each its own task:

1. **Freeze the engine** → `dist/autobot-daemon` (this doc's focus).
2. **Sidecar wiring** → `tauri.conf.json` `externalBin` + `main.rs` lifecycle.
3. **First-run wizard** → provider (Anthropic key *or* Ollama), STT-model download
   with progress, Mic/Automation permission prompts.

## Step 1 — freeze the engine

```bash
uv sync --extra freeze     # installs pyinstaller
make freeze                # -> dist/autobot-daemon  (pyinstaller packaging/autobot-daemon.spec)
./dist/autobot-daemon      # should serve on ws://127.0.0.1:8765, same as `autobot-daemon`
```

PyInstaller can't see the engine's **lazy imports** (heavy runtimes imported inside
functions), so `packaging/autobot-daemon.spec` lists them via `collect_all` /
`hiddenimports`. The first build on a Mac will almost certainly surface a few more:

- **`ModuleNotFoundError` at runtime** → add the module to `hiddenimports`.
- **missing `.dylib` / data file** (ctranslate2, onnxruntime, PortAudio via
  sounddevice, Piper) → add the package to the `collect_all` loop.

Iterate `make freeze` → run `./dist/autobot-daemon` → fix the spec → repeat until it
serves cleanly. (This back-and-forth is normal for native deps; send me the errors
and I'll adjust the spec.)

Scope for the bundle is the **try-it default stack**: cloud LLM (Anthropic) *or*
Ollama, faster-whisper STT, Piper TTS, sounddevice mic. The heavy optional extras
(whisper.cpp, openWakeWord/torch, VPIO AEC) stay out of the bundle for size; they
remain available in a `uv`/source install.

## Signing

Unsigned for the dev preview (Gatekeeper right-click → Open). Sign + notarize later
via Apple Developer secrets in the orb build (see `RELEASING.md`).
