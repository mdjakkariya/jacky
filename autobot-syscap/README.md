# autobot-syscap

A minimal SwiftPM CLI that captures **system audio output** (all processes, optionally excluding one by PID) via Core Audio process taps (macOS 14.2+) and writes raw PCM to stdout, so the Python `CoreAudioTapSource` in `src/autobot/io/system_audio_mac.py` can read it as a subprocess.

## Build

```bash
cd autobot-syscap
swift build -c release
# binary: .build/release/autobot-syscap
```

Requires macOS 14.2+ and Xcode Command Line Tools (Swift 5.9+). The binary must be **signed with the `com.apple.developer.audio-capture` entitlement** (or full-disk-access / TCC permission granted via System Preferences) before actual audio flows; unsigned builds pass TCC silently but deliver 0 bytes.

## Output contract

- **Invocation:** `autobot-syscap --sample-rate <int> --exclude-pid <int>`
  - `--sample-rate`: output PCM sample rate in Hz (default: 16000)
  - `--exclude-pid`: PID to exclude from the tap (e.g. Jack's own playback process; 0 = exclude nothing)
- **stdout:** raw little-endian 16-bit PCM, **mono**, at the requested sample rate — nothing else. The Python reader frames these bytes directly as int16 samples.
- **stderr:** diagnostics only. On successful start: `{"event":"started","sample_rate":16000,"channels":1}`. Errors are printed as `key=value` lines.
- **Exit codes:** 0 on clean shutdown (SIGTERM or SIGINT). Non-zero on failure (the Python side treats non-zero as a crash).
- Handles broken stdout pipe (parent died): ignores SIGPIPE and exits 0 on write failure.

## How it works

1. Creates a `CATapDescription` (stereo global tap, optionally excluding one process's `AudioObjectID`).
2. Calls `AudioHardwareCreateProcessTap` to get a tap capturing all system output.
3. Creates a private aggregate device wrapping the default output device + the tap (so real playback is not disrupted).
4. Registers an IOProc on the aggregate device. In the IOProc callback, each buffer of float32 multichannel audio at the native device rate (typically 44100/48000 Hz) is converted to 16 kHz mono int16 via `AVAudioConverter` and written to stdout.
5. Uses `DispatchSource` signal sources for SIGTERM/SIGINT (avoids Swift exclusivity violations from C signal handlers).
