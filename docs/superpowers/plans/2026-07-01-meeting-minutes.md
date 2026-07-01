# Meeting Minutes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Jack record both sides of a call, transcribe it to English on-device, and write structured minutes into a user-owned folder — reliably, for a call of any length.

**Architecture:** A parallel `meeting/` subsystem (not a new `AudioSource` in the turn loop). Gated tools start/stop/pause/resume it. While recording it only writes two 16 kHz mono WAVs to disk (near = mic, far = a signed Swift Core-Audio-tap sidecar). On stop it transcribes the WAVs once (the authoritative pass), interleaves the two streams by timestamp into `transcript.md`, then map-reduce-summarizes into `minutes.md`. The single mic is shared with the turn loop via a frame tee. Far-end capture degrades to mic-only if Audio-Capture is unavailable.

**Tech Stack:** Python ≥ 3.11 (daemon), Swift (the `autobot-syscap` sidecar), faster-whisper / whisper.cpp behind the existing `SpeechToText` protocol, Ollama/Anthropic behind `LanguageModel`, FastAPI daemon, Tauri `externalBin` bundling. Tests: pytest (no hardware, no model).

**Design reference:** [`docs/plans/autobot_meeting_minutes_plan.md`](../../plans/autobot_meeting_minutes_plan.md). Read it before starting — this plan implements that design.

## Global Constraints

Every task implicitly includes these (copied from `CLAUDE.md` and the design spec):

- **Python ≥ 3.11**, `from __future__ import annotations` at the top of every module.
- **mypy strict must stay green**; full type hints. Google-style docstrings on public modules/classes/functions (ruff `D` rules; tests are exempt). Line length 100. **Never hand-format — run `make format`.**
- **Value objects** are `@dataclass(frozen=True, slots=True)` with no business logic.
- **Tools return strings and never raise out of their handler** — errors become friendly strings.
- **On-device only.** No audio/transcript/summary leaves the machine. The cloud LLM (`llm_provider="anthropic"`) may be used for the *summary text only*, never audio — and only because it's the user's already-disclosed opt-in.
- **English only.** Every STT decode passes `language="en"`; never `translate`, never autodetect.
- **The permission gate is not optional.** Every acting tool goes through the registry + `PermissionGate`.
- **Heavy runtimes import lazily** (inside `__init__`/methods), so importing a module — and the test suite — stays fast.
- **`app.py::build()` is the only place that names concrete classes.**
- **Logging:** `from autobot.logging_setup import get_logger` then `_log = get_logger("meeting")` at module level; seam events only (never per-frame); `%`-style args; INFO for lifecycle, DEBUG for detail, `_log.exception` for failures.
- **Commit messages: Conventional Commits** (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). **Do NOT add a `Co-Authored-By` trailer** (repo convention).
- **Run `make check` (ruff + ruff-format + mypy strict + pytest) before every commit.**
- Pipeline sample rate is **16 kHz mono**; `AudioClip` is a 1-D `np.float32` array; mic frames are **512 samples** (`SAMPLE_RATE` / `CHANNELS` live in `config.py`).

## File Structure

**New files**
- `autobot-syscap/` — Swift package for the native Core-Audio-tap sidecar (Package.swift, Sources/…). Builds to a signed CLI binary.
- `src/autobot/io/system_audio_mac.py` — `CoreAudioTapSource` (spawns the sidecar) + pure `pcm16_to_frames` helper.
- `src/autobot/io/mic_tee.py` — `FrameTee` (one mic owner → N subscriber branches).
- `src/autobot/meeting/__init__.py`
- `src/autobot/meeting/wav.py` — `WavWriter` (crash-safe incremental WAV) + `repair_header`.
- `src/autobot/meeting/store.py` — `MeetingStore` (folder/slug, manifest, retention, recovery).
- `src/autobot/meeting/transcriber.py` — `MeetingTranscriber` + pure windowing/dedupe/merge helpers.
- `src/autobot/meeting/summarizer.py` — `MeetingSummarizer` (map-reduce, injected completer).
- `src/autobot/meeting/recorder.py` — `MeetingRecorder` (lifecycle, threads, finalize).
- `src/autobot/tools/meeting.py` — `MeetingTools` + `register_meeting_tools`.
- `tests/unit/test_*` — one per pure unit (see tasks).

**Modified files**
- `src/autobot/core/types.py` — add `Segment`.
- `src/autobot/core/interfaces.py` — add `transcribe_segments` to `SpeechToText`, add `SystemAudioSource`, add `complete` to `LanguageModel`.
- `src/autobot/stt/faster_whisper_stt.py`, `whisper_cpp_stt.py`, `reloadable.py` — implement `transcribe_segments`.
- `src/autobot/llm/ollama_llm.py`, `anthropic_llm.py`, `reloadable.py` — implement `complete`.
- `src/autobot/permissions.py` — add `AUDIO_CAPTURE`.
- `src/autobot/config.py` — add meeting settings.
- `src/autobot/core/events.py` — add `MeetingEvent` + `publish_meeting`.
- `src/autobot/daemon/server.py` — add `/meeting/*` routes.
- `src/autobot/app.py` — seed the sidecar, build the tee + source + recorder, register tools behind `allow_meetings`, wire the completer + meeting events.
- `src/autobot/tts/voices.py` (or a new `syscap.py`) — seed the bundled sidecar binary.
- `Makefile`, `packaging/autobot-daemon.spec`, `ui/orb-shell/src-tauri/tauri.conf.json` — build/bundle/sign the sidecar.

---

## Task 0: Native sidecar `autobot-syscap` (de-risk spike — build, sign, validate on hardware)

This is the one genuine unknown (design §16). Build and validate it **before** anything else. It cannot be proven in CI (no audio device); its "tests" are a manual hardware procedure.

**Files:**
- Create: `autobot-syscap/Package.swift`
- Create: `autobot-syscap/Sources/autobot-syscap/main.swift`
- Create: `autobot-syscap/Sources/autobot-syscap/SystemAudioTap.swift`
- Create: `autobot-syscap/Info.plist`
- Create: `autobot-syscap/README.md`

**Interfaces:**
- Produces: a CLI binary `autobot-syscap` that, given `--sample-rate 16000 --exclude-pid <pid>`, opens a Core Audio process tap on system output (excluding `<pid>`), and writes **little-endian 16-bit PCM, mono, 16 kHz** frames to **stdout**, with diagnostics/JSON metadata on **stderr**. Exits non-zero with a stderr reason on any failure (no permission, OS < 14.4, no audio).

- [ ] **Step 1: Scaffold the Swift package**

```swift
// autobot-syscap/Package.swift
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "autobot-syscap",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "autobot-syscap",
            path: "Sources/autobot-syscap"
        )
    ]
)
```

- [ ] **Step 2: Implement the tap** (`SystemAudioTap.swift`)

Model on AudioCap / AudioTee (see design references). The contract that matters downstream:
- Create a `CATapDescription` for **global output excluding** the PID passed via `--exclude-pid` (so Jack's own TTS never enters the capture). Set `isExclusive` correctly (exclude-list semantics).
- Build an aggregate device whose main sub-device is the real output, with the tap as a sub-tap.
- In the IO callback, **downmix to mono, resample to 16 kHz**, convert to **int16 LE**, and `FileHandle.standardOutput.write(...)` the raw bytes.
- On any `OSStatus != noErr`, print `key=value` diagnostics to stderr and `exit(1)`. Print a one-line JSON `{"event":"started","sample_rate":16000,"channels":1}` to stderr once capture begins.

```swift
// main.swift — argument parsing + lifecycle
import Foundation

var sampleRate = 16000
var excludePID: pid_t = 0
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let flag = args.removeFirst()
    switch flag {
    case "--sample-rate": sampleRate = Int(args.removeFirst()) ?? 16000
    case "--exclude-pid": excludePID = pid_t(args.removeFirst()) ?? 0
    default: FileHandle.standardError.write(Data("unknown flag: \(flag)\n".utf8)); exit(2)
    }
}
let tap = SystemAudioTap(sampleRate: sampleRate, excludePID: excludePID)
do { try tap.start() } catch {
    FileHandle.standardError.write(Data("error=\(error)\n".utf8)); exit(1)
}
// Run until stdin closes (parent died) or SIGTERM.
signal(SIGTERM) { _ in exit(0) }
RunLoop.main.run()
```

- [ ] **Step 3: Add the Info.plist with the usage string**

```xml
<!-- autobot-syscap/Info.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>NSAudioCaptureUsageDescription</key>
  <string>Jack records meeting audio so it can transcribe and summarize your call on-device.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Jack records your side of the meeting.</string>
  <key>LSMinimumSystemVersion</key><string>14.4</string>
</dict></plist>
```

- [ ] **Step 4: Build and sign**

```bash
cd autobot-syscap && swift build -c release
# Sign with a Developer ID so the Audio-Capture TCC prompt actually fires.
codesign --force --options runtime \
  --entitlements <(/usr/bin/plutil -create xml1 - 2>/dev/null; printf '%s' \
   '<?xml version="1.0"?><!DOCTYPE plist><plist version="1.0"><dict><key>com.apple.security.device.audio-input</key><true/></dict></plist>') \
  -s "Developer ID Application: <YOUR ID>" \
  .build/release/autobot-syscap
codesign -dv --verbose=4 .build/release/autobot-syscap   # confirm signed
```

- [ ] **Step 5: Manual hardware validation (the acceptance test)**

```bash
# Find Jack's audio-producing PID (the daemon) for exclusion; for the spike use 0.
# Join a Google Meet test call with audio playing, then:
.build/release/autobot-syscap --sample-rate 16000 --exclude-pid 0 > /tmp/far.raw 2>/tmp/far.log
# ...let it run ~30s, Ctrl-C, then convert raw PCM to a wav to listen:
ffmpeg -f s16le -ar 16000 -ac 1 -i /tmp/far.raw /tmp/far.wav && afplay /tmp/far.wav
```

Expected: `/tmp/far.log` shows `{"event":"started",...}`; the **first run triggers the macOS Audio-Capture permission prompt**; `/tmp/far.wav` contains the remote participants' voices and is clean. Confirm that with `--exclude-pid <daemon pid>`, audio the daemon plays is **absent** from the capture. Record the exact failure stderr for: permission denied, and running unsigned (prompt should NOT fire) — these strings inform Task 5's error mapping.

- [ ] **Step 6: Commit**

```bash
git add autobot-syscap/
git commit -m "feat(meeting): native autobot-syscap Core Audio tap sidecar"
```

---

## Task 1: `Segment` value object + `transcribe_segments` on the STT protocol

**Files:**
- Modify: `src/autobot/core/types.py` (after `Transcription`, ~line 57)
- Modify: `src/autobot/core/interfaces.py` (`SpeechToText`, ~line 42-55; imports ~line 20)
- Test: `tests/unit/test_segment_protocol.py`

**Interfaces:**
- Produces: `Segment(text: str, start: float, end: float)` (frozen/slots); `SpeechToText.transcribe_segments(audio, *, language="en", vad_filter=True, condition_on_previous_text=False, initial_prompt=None) -> list[Segment]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_segment_protocol.py
from __future__ import annotations

import numpy as np

from autobot.core.interfaces import SpeechToText
from autobot.core.types import Segment, Transcription


def test_segment_is_frozen_value_object() -> None:
    seg = Segment(text="hello", start=0.0, end=1.5)
    assert (seg.text, seg.start, seg.end) == ("hello", 0.0, 1.5)
    import pytest

    with pytest.raises(Exception):
        seg.text = "x"  # type: ignore[misc]


def test_fake_with_transcribe_segments_satisfies_protocol() -> None:
    class Fake:
        def transcribe(self, audio: np.ndarray) -> Transcription:
            return Transcription(text="", confidence=0.0)

        def transcribe_segments(
            self,
            audio: np.ndarray,
            *,
            language: str = "en",
            vad_filter: bool = True,
            condition_on_previous_text: bool = False,
            initial_prompt: str | None = None,
        ) -> list[Segment]:
            return [Segment(text="hi", start=0.0, end=0.4)]

    assert isinstance(Fake(), SpeechToText)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_segment_protocol.py -v`
Expected: FAIL — `ImportError: cannot import name 'Segment'`.

- [ ] **Step 3: Add `Segment` to `core/types.py`**

```python
@dataclass(frozen=True, slots=True)
class Segment:
    """One timestamped span of recognized speech (seconds from the stream start)."""

    text: str
    """The recognized text for this span, stripped. Never empty in a returned list."""

    start: float
    """Start time in seconds, relative to the start of the transcribed audio."""

    end: float
    """End time in seconds, relative to the start of the transcribed audio."""
```

- [ ] **Step 4: Add the protocol method to `core/interfaces.py`**

In the `TYPE_CHECKING` import block, add `Segment`:

```python
    from autobot.core.types import AudioClip, Segment, ToolExecutor, Transcription
```

Add to the `SpeechToText` protocol after `transcribe`:

```python
    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        """Transcribe a clip into timestamped :class:`Segment`s (English only).

        Used for long-form meeting audio, where the two streams must be merged by
        time. ``vad_filter`` skips silence; ``condition_on_previous_text=False``
        keeps one bad window from poisoning later ones. Segments are returned in
        time order with empty spans dropped.
        """
        ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_segment_protocol.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autobot/core/types.py src/autobot/core/interfaces.py tests/unit/test_segment_protocol.py
git commit -m "feat(meeting): Segment value object + transcribe_segments protocol method"
```

---

## Task 2: `FasterWhisperSTT.transcribe_segments` (+ pure helper)

**Files:**
- Modify: `src/autobot/stt/faster_whisper_stt.py`
- Test: `tests/unit/test_faster_whisper_segments.py`

**Interfaces:**
- Consumes: `Segment` (Task 1).
- Produces: module-level `segments_from_faster_whisper(raw) -> list[Segment]`; `FasterWhisperSTT.transcribe_segments(...)`.

- [ ] **Step 1: Write the failing test** (pure helper — no model)

```python
# tests/unit/test_faster_whisper_segments.py
from __future__ import annotations

from dataclasses import dataclass

from autobot.stt.faster_whisper_stt import segments_from_faster_whisper


@dataclass
class _Raw:
    text: str
    start: float
    end: float


def test_maps_and_strips_and_drops_empty() -> None:
    raw = [_Raw("  hello ", 0.0, 1.0), _Raw("   ", 1.0, 1.2), _Raw("world", 1.2, 2.0)]
    segs = segments_from_faster_whisper(raw)
    assert [(s.text, s.start, s.end) for s in segs] == [("hello", 0.0, 1.0), ("world", 1.2, 2.0)]


def test_empty_input() -> None:
    assert segments_from_faster_whisper([]) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_faster_whisper_segments.py -v`
Expected: FAIL — `ImportError: cannot import name 'segments_from_faster_whisper'`.

- [ ] **Step 3: Implement the helper and method**

Add the import at the top (`from autobot.core.types import AudioClip, Segment, Transcription`), then:

```python
def segments_from_faster_whisper(raw: object) -> list[Segment]:
    """Map faster-whisper segment objects to :class:`Segment`s, dropping empties."""
    out: list[Segment] = []
    for seg in raw or []:  # type: ignore[union-attr]
        text = str(getattr(seg, "text", "") or "").strip()
        if text:
            out.append(Segment(text=text, start=float(seg.start), end=float(seg.end)))
    return out
```

Add the method to `FasterWhisperSTT`:

```python
    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        """Long-form transcription into timestamped segments; see the interface."""
        if audio.size == 0:
            return []
        segments, _info = self._model.transcribe(
            audio,
            language="en",  # English-only: never autodetect, never translate
            beam_size=self._settings.stt_beam_size,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt if initial_prompt is not None else (self._settings.stt_prompt or None),
        )
        result = segments_from_faster_whisper(segments)
        _log.debug("transcribe_segments engine=faster_whisper segments=%d", len(result))
        return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_faster_whisper_segments.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/stt/faster_whisper_stt.py tests/unit/test_faster_whisper_segments.py
git commit -m "feat(meeting): faster-whisper transcribe_segments"
```

---

## Task 3: `WhisperCppSTT.transcribe_segments` (+ pure helper)

**Files:**
- Modify: `src/autobot/stt/whisper_cpp_stt.py`
- Test: `tests/unit/test_whisper_cpp_segments.py`

**Interfaces:**
- Produces: module-level `segments_from_cpp(raw) -> list[Segment]` (tolerant of `.t0/.t1` centisecond ints **or** `.start/.end` seconds, and dict or object — mirroring the existing `_seg_text` defensiveness); `WhisperCppSTT.transcribe_segments(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_whisper_cpp_segments.py
from __future__ import annotations

from autobot.stt.whisper_cpp_stt import segments_from_cpp


class _Seg:
    def __init__(self, text: str, t0: int, t1: int) -> None:
        self.text, self.t0, self.t1 = text, t0, t1  # t0/t1 in centiseconds


def test_centisecond_timing_objects() -> None:
    segs = segments_from_cpp([_Seg(" hi ", 0, 50), _Seg("  ", 50, 60), _Seg("there", 60, 130)])
    assert [(s.text, s.start, s.end) for s in segs] == [("hi", 0.0, 0.5), ("there", 0.6, 1.3)]


def test_dict_seconds_timing() -> None:
    segs = segments_from_cpp([{"text": "yo", "start": 1.0, "end": 2.0}])
    assert [(s.text, s.start, s.end) for s in segs] == [("yo", 1.0, 2.0)]


def test_empty() -> None:
    assert segments_from_cpp(None) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_whisper_cpp_segments.py -v`
Expected: FAIL — `ImportError: cannot import name 'segments_from_cpp'`.

- [ ] **Step 3: Implement the helper and method**

Add `Segment` and `AudioClip` to the imports, then:

```python
def _seg_time(segment: Any, key_cs: str, key_s: str) -> float:
    """Read a segment time in seconds, accepting centisecond ints (t0/t1) or seconds."""
    get = segment.get if isinstance(segment, dict) else lambda k, d=None: getattr(segment, k, d)
    cs = get(key_cs, None)
    if cs is not None:
        return float(cs) / 100.0  # whisper.cpp t0/t1 are centiseconds
    return float(get(key_s, 0.0) or 0.0)


def segments_from_cpp(raw: Any) -> list[Segment]:
    """Map whisper.cpp segments to :class:`Segment`s, dropping empties."""
    out: list[Segment] = []
    for seg in raw or []:
        text = _seg_text(seg).strip()
        if text:
            out.append(
                Segment(text=text, start=_seg_time(seg, "t0", "start"), end=_seg_time(seg, "t1", "end"))
            )
    return out
```

Add the method to `WhisperCppSTT` (whisper.cpp's binding has no `vad_filter`/`condition_on_previous_text` kwargs, so we accept-and-ignore them for protocol parity, gracefully falling back like the existing `transcribe`):

```python
    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        """Long-form transcription into timestamped segments; see the interface."""
        if audio.size == 0:
            return []
        prompt = initial_prompt if initial_prompt is not None else (self._settings.stt_prompt or "")
        try:
            segments = (
                self._model.transcribe(audio, language="en", initial_prompt=prompt)
                if prompt
                else self._model.transcribe(audio, language="en")
            )
        except TypeError:
            segments = self._model.transcribe(audio, language="en")
        result = segments_from_cpp(segments)
        _log.debug("transcribe_segments engine=whisper_cpp segments=%d", len(result))
        return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_whisper_cpp_segments.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/stt/whisper_cpp_stt.py tests/unit/test_whisper_cpp_segments.py
git commit -m "feat(meeting): whisper.cpp transcribe_segments"
```

---

## Task 4: `ReloadableSTT.transcribe_segments` forwarding

**Files:**
- Modify: `src/autobot/stt/reloadable.py`
- Test: `tests/unit/test_reloadable_segments.py`

**Interfaces:**
- Produces: `ReloadableSTT.transcribe_segments(...)` — same lazy-build/reload semantics as `transcribe`, forwarding to the inner engine.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reloadable_segments.py
from __future__ import annotations

import numpy as np

from autobot.core.types import Segment, Transcription
from autobot.stt.reloadable import ReloadableSTT


class _FakeSTT:
    def transcribe(self, audio):  # type: ignore[no-untyped-def]
        return Transcription(text="", confidence=0.0)

    def transcribe_segments(self, audio, **kw):  # type: ignore[no-untyped-def]
        return [Segment(text="ok", start=0.0, end=1.0)]


def test_forwards_and_lazy_builds() -> None:
    built = {"n": 0}

    def factory():  # type: ignore[no-untyped-def]
        built["n"] += 1
        return _FakeSTT()

    stt = ReloadableSTT(factory)
    out = stt.transcribe_segments(np.zeros(16000, dtype=np.float32), vad_filter=True)
    assert [s.text for s in out] == ["ok"]
    assert built["n"] == 1  # built lazily, once
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_reloadable_segments.py -v`
Expected: FAIL — `AttributeError: 'ReloadableSTT' object has no attribute 'transcribe_segments'`.

- [ ] **Step 3: Implement forwarding**

Add `Segment` to the imports. Refactor the lazy-build block into a private helper so both methods share it, then add the new method:

```python
    def _ensure(self) -> SpeechToText:
        """Build/reload the model on first use or after a settings change."""
        with self._lock:
            if self._inner is None or self._dirty:
                first = self._inner is None
                try:
                    self._inner = self._factory()
                    _log.info("stt model loaded" if first else "stt reloaded from updated settings")
                except Exception as exc:
                    if self._inner is None:
                        raise
                    _log.warning("stt reload failed, keeping current: %s", exc)
                self._dirty = False
            return self._inner

    def transcribe(self, audio: AudioClip) -> Transcription:
        """Build/reload the model on first use or after a settings change, then transcribe."""
        return self._ensure().transcribe(audio)

    def transcribe_segments(
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]:
        """Long-form transcription; same reload semantics as :meth:`transcribe`."""
        return self._ensure().transcribe_segments(
            audio,
            language=language,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
            initial_prompt=initial_prompt,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_reloadable_segments.py tests/unit/ -k stt -v`
Expected: PASS (and existing STT tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/autobot/stt/reloadable.py tests/unit/test_reloadable_segments.py
git commit -m "feat(meeting): ReloadableSTT forwards transcribe_segments"
```

---

## Task 5: `SystemAudioSource` protocol + `CoreAudioTapSource`

**Files:**
- Modify: `src/autobot/core/interfaces.py` (add protocol)
- Create: `src/autobot/io/system_audio_mac.py`
- Test: `tests/unit/test_pcm16_to_frames.py`

**Interfaces:**
- Produces: `SystemAudioSource` protocol (`frames() -> Iterator[AudioClip]`, `close() -> None`, attr `aec_active: bool`); `pcm16_to_frames(data: bytes, leftover: bytes, frame_samples: int = 512) -> tuple[list[AudioClip], bytes]`; `CoreAudioTapSource(binary_path, exclude_pid, sample_rate=16000)`.

- [ ] **Step 1: Add the protocol to `core/interfaces.py`**

Add `Iterator` import (`from collections.abc import Iterator` — guard under TYPE_CHECKING is unnecessary, it's stdlib) and:

```python
@runtime_checkable
class SystemAudioSource(Protocol):
    """Continuous far-end (system output) capture for meetings."""

    aec_active: bool
    """Parity with the mic source flags; always ``False`` for a system tap."""

    def frames(self) -> Iterator[AudioClip]:
        """Yield 512-sample 16 kHz mono ``float32`` frames until :meth:`close`."""
        ...

    def close(self) -> None:
        """Stop capture and release the sidecar. Idempotent."""
        ...
```

- [ ] **Step 2: Write the failing test for the pure converter**

```python
# tests/unit/test_pcm16_to_frames.py
from __future__ import annotations

import numpy as np

from autobot.io.system_audio_mac import pcm16_to_frames


def test_splits_into_full_frames_and_keeps_leftover() -> None:
    samples = np.array([0, 32767, -32768, 16384] * 200, dtype=np.int16)  # 800 samples
    frames, leftover = pcm16_to_frames(samples.tobytes(), b"", frame_samples=512)
    assert len(frames) == 1  # 800 // 512 = 1 full frame
    assert frames[0].dtype == np.float32 and frames[0].shape == (512,)
    assert abs(frames[0][1] - (32767 / 32768.0)) < 1e-4
    assert len(leftover) == (800 - 512) * 2  # remaining bytes carried over


def test_leftover_is_prepended() -> None:
    half = np.zeros(256, dtype=np.int16).tobytes()
    frames, leftover = pcm16_to_frames(half, half, frame_samples=512)  # 256 + 256 = 512
    assert len(frames) == 1 and leftover == b""
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/unit/test_pcm16_to_frames.py -v`
Expected: FAIL — module/function does not exist.

- [ ] **Step 4: Implement `system_audio_mac.py`**

```python
# src/autobot/io/system_audio_mac.py
"""Far-end (system output) capture via the native ``autobot-syscap`` Core Audio tap.

Spawns the bundled, signed Swift sidecar and reads little-endian int16 PCM frames
off its stdout. Lazy/guarded like :mod:`autobot.io.aec_mac`: any failure (helper
missing/unsigned, permission denied, OS too old, no audio) raises so the caller
degrades to mic-only rather than crashing. Validated manually on hardware — the
exact tap routing and the Audio-Capture prompt can't be exercised in CI.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import numpy as np

from autobot.core.types import AudioClip
from autobot.logging_setup import get_logger

_log = get_logger("meeting")

_INT16_SCALE = 32768.0


def pcm16_to_frames(
    data: bytes, leftover: bytes, frame_samples: int = 512
) -> tuple[list[AudioClip], bytes]:
    """Convert raw int16-LE bytes (+ any prior leftover) into full float32 frames.

    Returns the list of complete ``frame_samples``-length frames and the trailing
    bytes that didn't fill a frame, to be prepended next call.
    """
    buf = leftover + data
    nbytes = len(buf)
    frame_bytes = frame_samples * 2
    n_frames = nbytes // frame_bytes
    if n_frames == 0:
        return [], buf
    usable = n_frames * frame_bytes
    ints = np.frombuffer(buf[:usable], dtype="<i2").astype(np.float32) / _INT16_SCALE
    frames = [ints[i * frame_samples : (i + 1) * frame_samples] for i in range(n_frames)]
    return frames, buf[usable:]


class CoreAudioTapSource:
    """Reads far-end PCM frames from the ``autobot-syscap`` sidecar subprocess."""

    aec_active = False

    def __init__(self, binary_path: str, exclude_pid: int = 0, sample_rate: int = 16000) -> None:
        import subprocess

        self._stopped = threading.Event()
        _log.info("syscap spawning bin=%s exclude_pid=%d", binary_path, exclude_pid)
        self._proc = subprocess.Popen(
            [binary_path, "--sample-rate", str(sample_rate), "--exclude-pid", str(exclude_pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if self._proc.stdout is None:  # pragma: no cover - defensive
            raise RuntimeError("syscap: no stdout pipe")

    def frames(self) -> Iterator[AudioClip]:
        """Yield 512-sample float32 frames until the sidecar exits or :meth:`close`."""
        assert self._proc.stdout is not None
        leftover = b""
        while not self._stopped.is_set():
            chunk = self._proc.stdout.read(4096)
            if not chunk:  # EOF — sidecar exited (crash or stop)
                code = self._proc.poll()
                if code not in (0, None):
                    err = (self._proc.stderr.read().decode("utf-8", "replace") if self._proc.stderr else "")
                    _log.warning("syscap exited code=%s err=%s", code, err.strip()[:200])
                break
            new_frames, leftover = pcm16_to_frames(chunk, leftover)
            yield from new_frames

    def close(self) -> None:
        """Stop the sidecar and release it. Idempotent."""
        self._stopped.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        _log.info("syscap closed")
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/test_pcm16_to_frames.py -v`
Expected: PASS. (The subprocess path is validated manually in Task 0 / Task 16.)

- [ ] **Step 6: Commit**

```bash
git add src/autobot/core/interfaces.py src/autobot/io/system_audio_mac.py tests/unit/test_pcm16_to_frames.py
git commit -m "feat(meeting): SystemAudioSource protocol + CoreAudioTapSource"
```

---

## Task 6: `FrameTee` — share one mic across the turn loop and the recorder

**Files:**
- Create: `src/autobot/io/mic_tee.py`
- Test: `tests/unit/test_mic_tee.py`

**Interfaces:**
- Consumes: a `FrameSource`-like object (`frames() -> Iterator[AudioClip]`, `flush()`, optional `close()`).
- Produces: `FrameTee(source)`; `FrameTee.branch() -> _Branch` (each `_Branch` has `frames()` + `flush()`); `FrameTee.start()`, `FrameTee.close()`. One owner thread pulls `source.frames()` and copies each frame to every branch's bounded queue (dropping oldest on overflow so a slow consumer can't stall capture).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_mic_tee.py
from __future__ import annotations

import numpy as np

from autobot.io.mic_tee import FrameTee


class _FakeSource:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = frames

    def frames(self):  # type: ignore[no-untyped-def]
        yield from self._frames

    def flush(self) -> None:  # noqa: D401
        pass


def test_every_branch_receives_every_frame() -> None:
    data = [np.full(512, i, dtype=np.float32) for i in range(5)]
    tee = FrameTee(_FakeSource(data))
    a = tee.branch()
    b = tee.branch()
    tee.start()
    got_a = [int(f[0]) for f in _take(a, 5)]
    got_b = [int(f[0]) for f in _take(b, 5)]
    tee.close()
    assert got_a == [0, 1, 2, 3, 4]
    assert got_b == [0, 1, 2, 3, 4]


def _take(branch, n):  # type: ignore[no-untyped-def]
    out = []
    for f in branch.frames():
        out.append(f)
        if len(out) == n:
            break
    return out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_mic_tee.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `mic_tee.py`**

```python
# src/autobot/io/mic_tee.py
"""Fan one microphone frame stream out to multiple consumers.

The turn loop needs the mic (to hear "stop recording") and a meeting needs the
mic for the near end. Two opens of one device race, so a single owner thread
pulls the underlying ``FrameSource`` and copies each frame to every branch's
bounded queue. Branches that fall behind drop their oldest frame rather than
stall capture — the recorder's branch is drained promptly by a writer thread, so
in practice nothing is dropped.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator

from autobot.core.types import AudioClip
from autobot.logging_setup import get_logger

_log = get_logger("meeting")

_QUEUE_MAX = 256  # ~8s of 32ms frames; ample headroom for a prompt consumer


class _Branch:
    """One subscriber's view of the shared frame stream."""

    def __init__(self) -> None:
        self._q: queue.Queue[AudioClip | None] = queue.Queue(maxsize=_QUEUE_MAX)

    def _offer(self, frame: AudioClip | None) -> None:
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            self._q.put_nowait(frame)

    def frames(self) -> Iterator[AudioClip]:
        """Yield frames until the tee closes (a ``None`` sentinel ends iteration)."""
        while True:
            frame = self._q.get()
            if frame is None:
                return
            yield frame

    def flush(self) -> None:
        """Discard buffered frames (drop stale audio)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


class FrameTee:
    """Owns the mic ``FrameSource`` and fans its frames to branches."""

    def __init__(self, source: object) -> None:
        self._source = source
        self._branches: list[_Branch] = []
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def branch(self) -> _Branch:
        """Create a new subscriber branch (call before :meth:`start`)."""
        b = _Branch()
        self._branches.append(b)
        return b

    def start(self) -> None:
        """Begin pulling from the source on an owner thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="frame-tee", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for frame in self._source.frames():  # type: ignore[attr-defined]
                if self._stopped.is_set():
                    break
                for b in self._branches:
                    b._offer(frame)
        except Exception:
            _log.exception("frame tee source error")
        finally:
            for b in self._branches:
                b._offer(None)  # end every branch's iteration

    def close(self) -> None:
        """Stop the owner thread and close the underlying source. Idempotent."""
        self._stopped.set()
        close = getattr(self._source, "close", None)
        if callable(close):
            close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        for b in self._branches:
            b._offer(None)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_mic_tee.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/io/mic_tee.py tests/unit/test_mic_tee.py
git commit -m "feat(meeting): FrameTee fans the mic to turn loop + recorder"
```

---

## Task 7: crash-safe WAV writer (`meeting/wav.py`)

**Files:**
- Create: `src/autobot/meeting/__init__.py` (empty package marker with a one-line docstring)
- Create: `src/autobot/meeting/wav.py`
- Test: `tests/unit/test_wav_writer.py`

**Interfaces:**
- Produces: `WavWriter(path)` with `append(frame: AudioClip)`, `close()`, attr `data_bytes: int`; `repair_header(path: str) -> int` (patch RIFF/data sizes from file length; returns sample count); `read_wav(path) -> AudioClip` (test helper).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_wav_writer.py
from __future__ import annotations

import numpy as np

from autobot.meeting.wav import WavWriter, read_wav, repair_header


def test_write_read_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "near.wav"
    w = WavWriter(str(p))
    w.append(np.full(512, 0.5, dtype=np.float32))
    w.append(np.full(512, -0.5, dtype=np.float32))
    w.close()
    audio = read_wav(str(p))
    assert audio.shape == (1024,) and audio.dtype == np.float32
    assert abs(audio[0] - 0.5) < 1e-3 and abs(audio[600] + 0.5) < 1e-3


def test_repair_header_after_crash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "far.wav"
    w = WavWriter(str(p))
    w.append(np.zeros(512, dtype=np.float32))
    # Simulate a crash: bytes are on disk but close() never patched the sizes.
    raw = bytearray(p.read_bytes())
    raw[4:8] = b"\x00\x00\x00\x00"   # zero RIFF size
    raw[40:44] = b"\x00\x00\x00\x00"  # zero data size
    p.write_bytes(bytes(raw))
    n = repair_header(str(p))
    assert n == 512
    assert read_wav(str(p)).shape == (512,)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_wav_writer.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `meeting/__init__.py` and `wav.py`**

```python
# src/autobot/meeting/__init__.py
"""Meeting capture, transcription, and summarization subsystem."""
```

```python
# src/autobot/meeting/wav.py
"""Crash-safe incremental 16 kHz mono int16 WAV writing.

Writes a placeholder 44-byte header up front, appends frames as they arrive, and
patches the size fields on :meth:`close`. If the process dies before close, the
sizes stay zero — :func:`repair_header` rebuilds them from the file length, so a
hard crash still yields a transcribable file (design §5.5).
"""

from __future__ import annotations

import struct

import numpy as np

from autobot.core.types import AudioClip

_SAMPLE_RATE = 16000
_HEADER_BYTES = 44
_INT16_SCALE = 32767.0


def _header(data_len: int) -> bytes:
    """Build a canonical 44-byte PCM WAV header for ``data_len`` data bytes."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_len, b"WAVE", b"fmt ", 16, 1, 1, _SAMPLE_RATE,
        _SAMPLE_RATE * 2, 2, 16, b"data", data_len,
    )


class WavWriter:
    """Appends float32 frames to a WAV, keeping the header repairable on crash."""

    def __init__(self, path: str) -> None:
        self._f = open(path, "wb")  # noqa: SIM115 - long-lived, closed in close()
        self._f.write(_header(0))
        self.data_bytes = 0

    def append(self, frame: AudioClip) -> None:
        """Write one float32 frame as int16-LE PCM."""
        clipped = np.clip(frame, -1.0, 1.0)
        pcm = (clipped * _INT16_SCALE).astype("<i2").tobytes()
        self._f.write(pcm)
        self.data_bytes += len(pcm)

    def close(self) -> None:
        """Patch the size fields and close. Idempotent."""
        if self._f.closed:
            return
        self._f.seek(0)
        self._f.write(_header(self.data_bytes))
        self._f.close()


def repair_header(path: str) -> int:
    """Rebuild a WAV's RIFF/data sizes from the file length; return sample count."""
    import os

    size = os.path.getsize(path)
    data_len = max(0, size - _HEADER_BYTES)
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(_header(data_len))
    return data_len // 2


def read_wav(path: str) -> AudioClip:
    """Read a 16 kHz mono int16 WAV back into a float32 array (test/finalize helper)."""
    with open(path, "rb") as f:
        f.seek(_HEADER_BYTES)
        raw = f.read()
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_wav_writer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/meeting/__init__.py src/autobot/meeting/wav.py tests/unit/test_wav_writer.py
git commit -m "feat(meeting): crash-safe incremental WAV writer"
```

---

## Task 8: `MeetingStore` — folder/slug, manifest, retention, recovery

**Files:**
- Create: `src/autobot/meeting/store.py`
- Test: `tests/unit/test_meeting_store.py`

**Interfaces:**
- Produces:
  - `MeetingStore(meetings_dir: str, *, now: Callable[[], datetime] | None = None)`
  - `.create(title: str) -> MeetingPaths` where `MeetingPaths` is a frozen dataclass `{id, dir, near_wav, far_wav, transcript_md, minutes_md, manifest_json}`
  - `.write_manifest(paths, data: dict)`, `.read_manifest(meeting_dir: str) -> dict`
  - `.list_recent() -> list[dict]` (manifests, newest first)
  - `.prune(keep: int) -> list[str]` (deletes oldest beyond `keep`, returns removed ids; never the active one if state in non-terminal)
  - `.find_interrupted() -> list[str]` (dirs whose state ∈ {recording, paused, transcribing, summarizing})
- Non-terminal states constant: `ACTIVE_STATES = {"recording", "paused", "transcribing", "summarizing"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_meeting_store.py
from __future__ import annotations

from datetime import datetime

from autobot.meeting.store import MeetingStore


def _store(tmp_path, t=datetime(2026, 6, 30, 10, 15)):  # type: ignore[no-untyped-def]
    return MeetingStore(str(tmp_path), now=lambda: t)


def test_create_makes_slugged_folder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _store(tmp_path).create("Daily Standup!")
    assert paths.id == "2026-06-30-1015-daily-standup"
    assert paths.dir.endswith("2026-06-30-1015-daily-standup")
    assert paths.near_wav.endswith("near.wav") and paths.far_wav.endswith("far.wav")


def test_manifest_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    paths = store.create("x")
    store.write_manifest(paths, {"id": paths.id, "state": "done", "title": "x"})
    assert store.read_manifest(paths.dir)["state"] == "done"


def test_find_interrupted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    p = store.create("m")
    store.write_manifest(p, {"id": p.id, "state": "recording"})
    assert store.find_interrupted() == [p.id]
    store.write_manifest(p, {"id": p.id, "state": "done"})
    assert store.find_interrupted() == []


def test_prune_keeps_most_recent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = MeetingStore(str(tmp_path))
    ids = []
    for i in range(5):
        p = store.create(f"m{i}")
        store.write_manifest(p, {"id": p.id, "state": "done"})
        ids.append(p.id)
    removed = store.prune(keep=2)
    assert len(removed) == 3
    assert len(store.list_recent()) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_store.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `store.py`**

```python
# src/autobot/meeting/store.py
"""Per-meeting folder, manifest, retention, and crash recovery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autobot.logging_setup import get_logger

_log = get_logger("meeting")

ACTIVE_STATES = {"recording", "paused", "transcribing", "summarizing"}


@dataclass(frozen=True, slots=True)
class MeetingPaths:
    """Absolute paths for one meeting's artifacts."""

    id: str
    dir: str
    near_wav: str
    far_wav: str
    transcript_md: str
    minutes_md: str
    manifest_json: str


def _slug(title: str) -> str:
    """A filesystem-safe, lowercased slug from a title (empty -> 'meeting')."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "meeting"


class MeetingStore:
    """Creates/locates meeting folders and manages manifests + retention."""

    def __init__(self, meetings_dir: str, *, now: Callable[[], datetime] | None = None) -> None:
        self._root = Path(meetings_dir).expanduser()
        self._now = now or datetime.now

    def _paths(self, meeting_id: str) -> MeetingPaths:
        d = self._root / meeting_id
        return MeetingPaths(
            id=meeting_id,
            dir=str(d),
            near_wav=str(d / "near.wav"),
            far_wav=str(d / "far.wav"),
            transcript_md=str(d / "transcript.md"),
            minutes_md=str(d / "minutes.md"),
            manifest_json=str(d / "manifest.json"),
        )

    def create(self, title: str) -> MeetingPaths:
        """Create a new meeting folder named ``YYYY-MM-DD-HHMM-<slug>``."""
        stamp = self._now().strftime("%Y-%m-%d-%H%M")
        meeting_id = f"{stamp}-{_slug(title)}"
        paths = self._paths(meeting_id)
        Path(paths.dir).mkdir(parents=True, exist_ok=True)
        _log.info("meeting folder created id=%s", meeting_id)
        return paths

    def write_manifest(self, paths: MeetingPaths, data: dict[str, object]) -> None:
        """Write the manifest JSON atomically."""
        tmp = Path(paths.manifest_json + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(paths.manifest_json)

    def read_manifest(self, meeting_dir: str) -> dict[str, object]:
        """Read a manifest; ``{}`` if missing/malformed."""
        p = Path(meeting_dir) / "manifest.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _all_dirs(self) -> list[Path]:
        if not self._root.exists():
            return []
        return sorted((d for d in self._root.iterdir() if d.is_dir()), reverse=True)

    def list_recent(self) -> list[dict[str, object]]:
        """All meetings' manifests, newest first (by folder name = timestamped id)."""
        return [m for d in self._all_dirs() if (m := self.read_manifest(str(d)))]

    def find_interrupted(self) -> list[str]:
        """Ids of meetings left in a non-terminal state (to finalize on startup)."""
        out: list[str] = []
        for d in self._all_dirs():
            state = str(self.read_manifest(str(d)).get("state", ""))
            if state in ACTIVE_STATES:
                out.append(d.name)
        return out

    def prune(self, keep: int) -> list[str]:
        """Delete oldest meetings beyond ``keep`` (never an active one)."""
        import shutil

        removed: list[str] = []
        dirs = self._all_dirs()  # newest first
        for d in dirs[keep:]:
            state = str(self.read_manifest(str(d)).get("state", ""))
            if state in ACTIVE_STATES:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        if removed:
            _log.info("meeting retention pruned count=%d", len(removed))
        return removed
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_meeting_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/meeting/store.py tests/unit/test_meeting_store.py
git commit -m "feat(meeting): MeetingStore folder/manifest/retention/recovery"
```

---

## Task 9: `MeetingTranscriber` — windowing, overlap-dedupe, two-stream merge

**Files:**
- Create: `src/autobot/meeting/transcriber.py`
- Test: `tests/unit/test_meeting_transcriber.py`

**Interfaces:**
- Consumes: `Segment` (Task 1), `SpeechToText.transcribe_segments` (Tasks 2–4), `read_wav` (Task 7).
- Produces:
  - pure `plan_windows(total_s: float, chunk_s: float, overlap_s: float) -> list[tuple[float, float]]`
  - pure `dedupe_overlap(segments: list[Segment]) -> list[Segment]` (sort by start; drop a segment whose `start` is within an existing segment's span with equal text)
  - pure `merge_streams(near: list[Segment], far: list[Segment]) -> list[tuple[str, Segment]]` (chronological; tag `"you"` / `"participants"`)
  - pure `render_transcript(lines: list[tuple[str, Segment]], *, mic_only: bool) -> str`
  - `MeetingTranscriber(stt, *, chunk_s, overlap_s, stt_prompt)` with `.transcribe_stream(wav_path: str) -> list[Segment]` and `.build(near_wav, far_wav | None, mic_only) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_meeting_transcriber.py
from __future__ import annotations

from autobot.core.types import Segment
from autobot.meeting.transcriber import (
    dedupe_overlap,
    merge_streams,
    plan_windows,
    render_transcript,
)


def test_plan_windows_overlap() -> None:
    assert plan_windows(70.0, 30.0, 3.0) == [(0.0, 30.0), (27.0, 57.0), (54.0, 70.0)]


def test_plan_windows_short_audio() -> None:
    assert plan_windows(12.0, 30.0, 3.0) == [(0.0, 12.0)]


def test_dedupe_overlap_drops_repeated_boundary_word() -> None:
    segs = [Segment("hello there", 0.0, 2.0), Segment("hello there", 1.9, 2.1), Segment("next", 5.0, 6.0)]
    out = dedupe_overlap(segs)
    assert [s.text for s in out] == ["hello there", "next"]


def test_merge_tags_and_orders() -> None:
    near = [Segment("hi", 0.0, 1.0), Segment("bye", 5.0, 6.0)]
    far = [Segment("hello", 2.0, 3.0)]
    lines = merge_streams(near, far)
    assert [(who, s.text) for who, s in lines] == [
        ("you", "hi"), ("participants", "hello"), ("you", "bye"),
    ]


def test_render_marks_mic_only() -> None:
    out = render_transcript([("you", Segment("hi", 0.0, 1.0))], mic_only=True)
    assert "[you]" in out and "mic-only" in out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_transcriber.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `transcriber.py`**

```python
# src/autobot/meeting/transcriber.py
"""Turn the on-disk WAVs into a merged, speaker-tagged transcript (design §5.3)."""

from __future__ import annotations

from autobot.core.interfaces import SpeechToText
from autobot.core.types import Segment
from autobot.logging_setup import get_logger
from autobot.meeting.wav import read_wav

_log = get_logger("meeting")
_SAMPLE_RATE = 16000


def plan_windows(total_s: float, chunk_s: float, overlap_s: float) -> list[tuple[float, float]]:
    """Split ``total_s`` into ``chunk_s`` windows that overlap by ``overlap_s``."""
    if total_s <= chunk_s:
        return [(0.0, total_s)]
    step = chunk_s - overlap_s
    out: list[tuple[float, float]] = []
    start = 0.0
    while start < total_s:
        end = min(start + chunk_s, total_s)
        out.append((round(start, 3), round(end, 3)))
        if end >= total_s:
            break
        start += step
    return out


def dedupe_overlap(segments: list[Segment]) -> list[Segment]:
    """Drop near-duplicate segments produced in overlap regions (same text, overlapping)."""
    ordered = sorted(segments, key=lambda s: s.start)
    out: list[Segment] = []
    for seg in ordered:
        if out and seg.text == out[-1].text and seg.start < out[-1].end + 0.5:
            continue
        out.append(seg)
    return out


def merge_streams(near: list[Segment], far: list[Segment]) -> list[tuple[str, Segment]]:
    """Interleave the two streams chronologically, tagging speaker side."""
    tagged = [("you", s) for s in near] + [("participants", s) for s in far]
    tagged.sort(key=lambda pair: pair[1].start)
    return tagged


def _stamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(lines: list[tuple[str, Segment]], *, mic_only: bool) -> str:
    """Render the merged lines to markdown."""
    head = "# Transcript\n"
    if mic_only:
        head += "\n> Recorded mic-only — the other participants' audio was not captured.\n"
    body = "\n".join(f"`{_stamp(s.start)}` **[{who}]** {s.text}" for who, s in lines)
    return f"{head}\n{body}\n"


class MeetingTranscriber:
    """Transcribes each WAV in bounded windows and merges them."""

    def __init__(
        self, stt: SpeechToText, *, chunk_s: float, overlap_s: float, stt_prompt: str
    ) -> None:
        self._stt = stt
        self._chunk_s = chunk_s
        self._overlap_s = overlap_s
        self._prompt = stt_prompt

    def transcribe_stream(self, wav_path: str) -> list[Segment]:
        """Transcribe one WAV, windowed to bound memory, deduping the overlaps."""
        audio = read_wav(wav_path)
        total_s = len(audio) / _SAMPLE_RATE
        collected: list[Segment] = []
        for start_s, end_s in plan_windows(total_s, self._chunk_s, self._overlap_s):
            window = audio[int(start_s * _SAMPLE_RATE) : int(end_s * _SAMPLE_RATE)]
            for seg in self._stt.transcribe_segments(
                window, language="en", vad_filter=True, condition_on_previous_text=False,
                initial_prompt=self._prompt or None,
            ):
                collected.append(Segment(seg.text, seg.start + start_s, seg.end + start_s))
        deduped = dedupe_overlap(collected)
        _log.info("transcribe stream=%s windows=%d segments=%d", wav_path, 0, len(deduped))
        return deduped

    def build(self, near_wav: str, far_wav: str | None, *, mic_only: bool) -> str:
        """Transcribe both streams (or just near) and render the merged transcript."""
        near = self.transcribe_stream(near_wav)
        far = self.transcribe_stream(far_wav) if far_wav and not mic_only else []
        lines = merge_streams(near, far)
        return render_transcript(lines, mic_only=mic_only)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_meeting_transcriber.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/meeting/transcriber.py tests/unit/test_meeting_transcriber.py
git commit -m "feat(meeting): MeetingTranscriber windowing/dedupe/merge"
```

---

## Task 10: `LanguageModel.complete` (no-tools completion for summaries)

The summary is a single non-conversational call. Rather than route through `run_turn` (which advertises tools and needs an executor), add one additive method to the `LanguageModel` protocol and both concrete clients. This keeps the provider abstraction (and the cloud opt-in routing) intact.

**Files:**
- Modify: `src/autobot/core/interfaces.py` (`LanguageModel`)
- Modify: `src/autobot/llm/ollama_llm.py`, `src/autobot/llm/anthropic_llm.py`, `src/autobot/llm/reloadable.py`
- Test: `tests/unit/test_language_model_complete.py`

**Interfaces:**
- Produces: `LanguageModel.complete(prompt: str, *, temperature: float = 0.0) -> str`.

> **Implementer note:** open `ollama_llm.py` and `anthropic_llm.py` first and reuse their *existing* client handle and model field — the code below shows the canonical call shape; match the actual attribute names (e.g. `self._client`, `self._settings.llm_model`) used in each file.

- [ ] **Step 1: Write the failing test** (protocol + a fake)

```python
# tests/unit/test_language_model_complete.py
from __future__ import annotations

from autobot.core.interfaces import LanguageModel


def test_fake_with_complete_satisfies_protocol() -> None:
    class Fake:
        def run_turn(self, user_text, execute):  # type: ignore[no-untyped-def]
            return ""

        def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
            return f"summary of {len(prompt)} chars"

    lm = Fake()
    assert isinstance(lm, LanguageModel)
    assert lm.complete("hello") == "summary of 5 chars"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_language_model_complete.py -v`
Expected: FAIL — `isinstance` is False (protocol lacks `complete`) — actually it passes isinstance only after the method is added; assert the test fails because `complete` isn't yet part of the protocol's runtime check. (If green prematurely, add a second fake missing `complete` and assert `not isinstance`.)

- [ ] **Step 3: Add `complete` to the protocol**

```python
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot, non-conversational completion (no tools). Used for summaries."""
        ...
```

- [ ] **Step 4: Implement in `ollama_llm.py`** (match the existing client/model handles)

```python
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion via Ollama chat (no tools advertised)."""
        resp = self._client.chat(
            model=self._settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": temperature},
            think=False,
        )
        return str(resp["message"]["content"]).strip()
```

- [ ] **Step 5: Implement in `anthropic_llm.py`** (match the existing client/model handles)

```python
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """One-shot completion via the Anthropic Messages API (no tools)."""
        resp = self._client.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=self._settings.anthropic_max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()
```

- [ ] **Step 6: Forward in `llm/reloadable.py`** (mirror its existing `run_turn` forwarding)

```python
    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """Forward to the (lazily built) inner model; same reload semantics as run_turn."""
        return self._ensure().complete(prompt, temperature=temperature)
```

(If `ReloadableLanguageModel` doesn't already have an `_ensure()` helper, refactor its lazy-build block into one exactly as Task 4 did for STT, then call it from both `run_turn` and `complete`.)

- [ ] **Step 7: Run to verify it passes**

Run: `uv run pytest tests/unit/test_language_model_complete.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/autobot/core/interfaces.py src/autobot/llm/ tests/unit/test_language_model_complete.py
git commit -m "feat(meeting): LanguageModel.complete for non-conversational summaries"
```

---

## Task 11: `MeetingSummarizer` — map-reduce minutes

> **Correction (applied during implementation):** the `_reduce` shown below has an
> infinite-recursion hazard — re-chunking the joined notes character-wise lets a
> non-shrinking completer *grow* the note count forever. The shipped code instead
> **batches whole notes** (`batch_notes`), caps rounds (`_MAX_REDUCE_ROUNDS`), and
> bails to a truncated join on no progress, so it terminates for any completer.
> See `src/autobot/meeting/summarizer.py` for the authoritative implementation.

**Files:**
- Create: `src/autobot/meeting/summarizer.py`
- Test: `tests/unit/test_meeting_summarizer.py`

**Interfaces:**
- Consumes: a `Completer = Callable[[str], str]` (wired in build() to `lambda p: language_model.complete(p)`).
- Produces:
  - pure `chunk_text(text: str, max_chars: int) -> list[str]` (split on blank lines / line boundaries, never exceeding `max_chars`)
  - `MeetingSummarizer(complete: Completer, *, max_chars: int)` with `.summarize(transcript: str, *, title: str, date: str, duration: str, mic_only: bool) -> str`
  - map → per-chunk notes; reduce → final minutes; recurse the reduce when combined notes exceed `max_chars`.

- [ ] **Step 1: Write the failing test** (fake completer — no model)

```python
# tests/unit/test_meeting_summarizer.py
from __future__ import annotations

from autobot.meeting.summarizer import MeetingSummarizer, chunk_text


def test_chunk_text_respects_max() -> None:
    text = "\n".join(f"line {i}" for i in range(20))
    chunks = chunk_text(text, max_chars=30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_map_reduce_calls_completer_per_chunk_then_reduces() -> None:
    calls: list[str] = []

    def fake_complete(prompt: str) -> str:
        calls.append(prompt)
        return f"NOTE({len(calls)})"

    big = "\n".join(f"sentence number {i}" for i in range(50))
    s = MeetingSummarizer(fake_complete, max_chars=80)
    out = s.summarize(big, title="Standup", date="2026-06-30", duration="12m", mic_only=False)
    assert "Standup" in out and "2026-06-30" in out
    assert len(calls) >= 2  # at least one map call per chunk + a reduce call


def test_recurses_when_notes_overflow() -> None:
    s = MeetingSummarizer(lambda p: "x" * 200, max_chars=100)  # every note overflows
    out = s.summarize("a\n" * 300, title="T", date="D", duration="1m", mic_only=False)
    assert out  # terminates (recursion bottoms out) and returns a string
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_summarizer.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `summarizer.py`**

```python
# src/autobot/meeting/summarizer.py
"""Hierarchical map-reduce minutes from a (possibly very long) transcript (design §7.1)."""

from __future__ import annotations

from collections.abc import Callable

from autobot.logging_setup import get_logger

_log = get_logger("meeting")

Completer = Callable[[str], str]

_MAP_PROMPT = (
    "You are summarizing one part of a meeting transcript. Extract, in English, "
    "concise notes as bullet points covering: key points, decisions, and action "
    "items (with the owner's name when the transcript names them). Be faithful; "
    "invent nothing.\n\nTRANSCRIPT PART:\n{chunk}"
)
_REDUCE_PROMPT = (
    "Combine these per-part meeting notes into one set of consolidated notes with "
    "the same three categories (key points, decisions, action items with owners). "
    "Merge duplicates.\n\nNOTES:\n{notes}"
)
_FINAL_PROMPT = (
    "Write the final meeting minutes in English from these consolidated notes. "
    "Use exactly these markdown sections: '## Summary' (a short prose paragraph), "
    "'## Decisions' (bullets), '## Action items' (bullets, each '- owner — task' "
    "when an owner is named, else '- task'), '## Open questions' (bullets). If a "
    "section has nothing, write '- None'.\n\nNOTES:\n{notes}"
)


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text on line boundaries into chunks no larger than ``max_chars``."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


class MeetingSummarizer:
    """Builds structured minutes via map-reduce over the transcript."""

    def __init__(self, complete: Completer, *, max_chars: int) -> None:
        self._complete = complete
        self._max_chars = max(1, max_chars)

    def _reduce(self, notes: list[str]) -> str:
        """Combine per-chunk notes, recursing while they overflow one window."""
        combined = "\n\n".join(notes)
        if len(combined) <= self._max_chars or len(notes) <= 1:
            return combined
        groups = chunk_text(combined, self._max_chars)
        _log.info("summarize reduce groups=%d", len(groups))
        return self._reduce([self._complete(_REDUCE_PROMPT.format(notes=g)) for g in groups])

    def summarize(
        self, transcript: str, *, title: str, date: str, duration: str, mic_only: bool
    ) -> str:
        """Return the full ``minutes.md`` body."""
        chunks = chunk_text(transcript, self._max_chars)
        _log.info("summarize map chunks=%d", len(chunks))
        notes = [self._complete(_MAP_PROMPT.format(chunk=c)) for c in chunks]
        consolidated = self._reduce(notes)
        body = self._complete(_FINAL_PROMPT.format(notes=consolidated))
        sides = "You only (mic-only)" if mic_only else "You and the call participants"
        return (
            f"# {title}\n\n"
            f"- **Date:** {date}\n- **Duration:** {duration}\n- **Attendees:** {sides}\n\n"
            f"{body}\n"
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_meeting_summarizer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/meeting/summarizer.py tests/unit/test_meeting_summarizer.py
git commit -m "feat(meeting): MeetingSummarizer map-reduce minutes"
```

---

## Task 12: `MeetingRecorder` — lifecycle, capture threads, finalize

**Files:**
- Create: `src/autobot/meeting/recorder.py`
- Test: `tests/unit/test_meeting_recorder.py`

**Interfaces:**
- Consumes: `MeetingStore` (8), `WavWriter` (7), `MeetingTranscriber` (9), `MeetingSummarizer` (11), a near `FrameSource`-branch factory and a far `SystemAudioSource` factory.
- Produces: `MeetingRecorder(store, transcriber, summarizer, *, near_branch_factory, far_source_factory, keep_audio, on_event=None, keep=20)` with:
  - `.start(title: str) -> str` (begins capture; returns a spoken-friendly ack incl. a mic-only warning if far failed; refuses if already active)
  - `.pause() -> str`, `.resume() -> str`
  - `.stop() -> str` (stop capture → transcribe → summarize → write files → done; returns "Saved …")
  - `.status() -> dict` (`{active, paused, mic_only, elapsed_s, recorded_s, title, state}`)
  - `.finalize_interrupted() -> list[str]` (recover on startup)
- `near_branch_factory: Callable[[], object]` returns an object with `frames()`; `far_source_factory: Callable[[], SystemAudioSource]` raises on far-end failure.

- [ ] **Step 1: Write the failing test** (fakes — no hardware, no model)

```python
# tests/unit/test_meeting_recorder.py
from __future__ import annotations

import numpy as np

from autobot.core.types import Segment
from autobot.meeting.recorder import MeetingRecorder
from autobot.meeting.store import MeetingStore


class _FakeBranch:
    def __init__(self, n: int) -> None:
        self._frames = [np.full(512, 0.1, dtype=np.float32) for _ in range(n)]

    def frames(self):  # type: ignore[no-untyped-def]
        yield from self._frames


class _FakeFar(_FakeBranch):
    aec_active = False

    def close(self) -> None:  # noqa: D401
        pass


class _FakeSTT:
    def transcribe_segments(self, audio, **kw):  # type: ignore[no-untyped-def]
        return [Segment("hello world", 0.0, 1.0)] if audio.size else []


class _FakeSummarizer:
    def summarize(self, transcript, **kw):  # type: ignore[no-untyped-def]
        return "## Summary\n- ok\n"


def _recorder(tmp_path, far_ok=True):  # type: ignore[no-untyped-def]
    from autobot.meeting.transcriber import MeetingTranscriber

    store = MeetingStore(str(tmp_path))
    tr = MeetingTranscriber(_FakeSTT(), chunk_s=30.0, overlap_s=3.0, stt_prompt="")

    def far_factory():  # type: ignore[no-untyped-def]
        if not far_ok:
            raise RuntimeError("audio capture denied")
        return _FakeFar(8)

    return MeetingRecorder(
        store, tr, _FakeSummarizer(),
        near_branch_factory=lambda: _FakeBranch(8),
        far_source_factory=far_factory,
        keep_audio=True,
    )


def test_full_lifecycle_writes_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rec = _recorder(tmp_path)
    ack = rec.start("Standup")
    assert "recording" in ack.lower()
    assert rec.status()["active"] is True and rec.status()["mic_only"] is False
    out = rec.stop()
    assert "saved" in out.lower()
    assert rec.status()["active"] is False


def test_degrades_to_mic_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rec = _recorder(tmp_path, far_ok=False)
    ack = rec.start("Solo")
    assert "your side" in ack.lower() or "mic-only" in ack.lower()
    assert rec.status()["mic_only"] is True
    rec.stop()


def test_refuses_double_start(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rec = _recorder(tmp_path)
    rec.start("A")
    assert "already" in rec.start("B").lower()
    rec.stop()


def test_pause_resume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rec = _recorder(tmp_path)
    rec.start("A")
    assert "paused" in rec.pause().lower()
    assert "paused" in rec.pause().lower()  # idempotent message
    assert "resum" in rec.resume().lower()
    rec.stop()


def test_stop_with_nothing_active(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert "no meeting" in _recorder(tmp_path).stop().lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_recorder.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `recorder.py`**

```python
# src/autobot/meeting/recorder.py
"""Owns a meeting's capture threads and the stop→transcribe→summarize finalize.

Capture writes only to disk (no STT/LLM while recording), so a long meeting costs
constant RAM and survives a crash. Sources + transcriber + summarizer are injected
so this is unit-tested with fakes (design §4.2, §5).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from autobot.core.interfaces import SystemAudioSource
from autobot.logging_setup import get_logger
from autobot.meeting.store import MeetingPaths, MeetingStore
from autobot.meeting.summarizer import MeetingSummarizer
from autobot.meeting.transcriber import MeetingTranscriber
from autobot.meeting.wav import WavWriter, repair_header

_log = get_logger("meeting")


class _StreamWriter:
    """Drains a frame iterator into a WavWriter on its own thread, pausable."""

    def __init__(self, source: object, wav_path: str) -> None:
        self._source = source
        self._writer = WavWriter(wav_path)
        self._paused = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            for frame in self._source.frames():  # type: ignore[attr-defined]
                if self._stopped.is_set():
                    break
                if not self._paused.is_set():
                    self._writer.append(frame)
        except Exception:
            _log.exception("stream writer error path=%s", self._writer)
        finally:
            self._writer.close()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        self._stopped.set()
        close = getattr(self._source, "close", None)
        if callable(close):
            close()
        self._thread.join(timeout=3)
        self._writer.close()


class MeetingRecorder:
    """Start/stop/pause/resume a meeting and finalize it on stop."""

    def __init__(
        self,
        store: MeetingStore,
        transcriber: MeetingTranscriber,
        summarizer: MeetingSummarizer,
        *,
        near_branch_factory: Callable[[], object],
        far_source_factory: Callable[[], SystemAudioSource],
        keep_audio: bool,
        keep: int = 20,
        on_event: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._store = store
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._near_factory = near_branch_factory
        self._far_factory = far_source_factory
        self._keep_audio = keep_audio
        self._keep = keep
        self._on_event = on_event
        self._lock = threading.Lock()
        self._active: _Active | None = None

    def _emit(self) -> None:
        if self._on_event is not None:
            self._on_event(self.status())

    def start(self, title: str) -> str:
        """Begin capture; degrade to mic-only if the far end can't start."""
        with self._lock:
            if self._active is not None:
                return "A meeting is already recording. Say 'stop recording' to finish it first."
            paths = self._store.create(title)
            started = datetime.now()
            near_writer = _StreamWriter(self._near_factory(), paths.near_wav)
            far_writer: _StreamWriter | None = None
            mic_only = False
            far_reason = ""
            try:
                far_writer = _StreamWriter(self._far_factory(), paths.far_wav)
            except Exception as exc:
                mic_only = True
                far_reason = str(exc)
                _log.warning("far-end unavailable, mic-only: %s", far_reason)
            self._active = _Active(
                paths=paths, title=title or "Meeting", started=started,
                near=near_writer, far=far_writer, mic_only=mic_only,
            )
            near_writer.start()
            if far_writer is not None:
                far_writer.start()
            self._write_manifest("recording")
            _log.info("meeting start id=%s mic_only=%s", paths.id, mic_only)
        self._emit()
        if mic_only:
            return (
                "Recording the meeting — but I can only hear your side; the other "
                "participants' audio isn't being captured."
            )
        return "Recording the meeting."

    def pause(self) -> str:
        with self._lock:
            if self._active is None:
                return "No meeting is recording."
            if self._active.paused:
                return "The meeting is already paused."
            self._active.paused = True
            self._active.pauses.append({"at": datetime.now().isoformat()})
            self._active.near.pause()
            if self._active.far is not None:
                self._active.far.pause()
            self._write_manifest("paused")
        self._emit()
        return "Paused the recording."

    def resume(self) -> str:
        with self._lock:
            if self._active is None:
                return "No meeting is recording."
            if not self._active.paused:
                return "The meeting isn't paused."
            self._active.paused = False
            if self._active.pauses:
                self._active.pauses[-1]["resumed_at"] = datetime.now().isoformat()
            self._active.near.resume()
            if self._active.far is not None:
                self._active.far.resume()
            self._write_manifest("recording")
        self._emit()
        return "Resumed the recording."

    def stop(self) -> str:
        with self._lock:
            active = self._active
            if active is None:
                return "No meeting is recording."
            self._active = None
        active.near.stop()
        if active.far is not None:
            active.far.stop()
        self._finalize(active)
        self._store.prune(self._keep)
        self._emit()
        return f"Saved the meeting minutes to {active.paths.dir}."

    def _finalize(self, active: _Active) -> None:
        """Transcribe → summarize → write files; recover headers first."""
        self._write_manifest_for(active, "transcribing")
        self._emit()
        repair_header(active.paths.near_wav)
        if active.far is not None:
            repair_header(active.paths.far_wav)
        transcript = self._transcriber.build(
            active.paths.near_wav,
            active.paths.far_wav if active.far is not None else None,
            mic_only=active.mic_only,
        )
        with open(active.paths.transcript_md, "w", encoding="utf-8") as f:
            f.write(transcript)
        self._write_manifest_for(active, "summarizing")
        self._emit()
        duration = _fmt_duration((datetime.now() - active.started).total_seconds())
        try:
            minutes = self._summarizer.summarize(
                transcript, title=active.title, date=active.started.strftime("%Y-%m-%d"),
                duration=duration, mic_only=active.mic_only,
            )
        except Exception as exc:
            _log.exception("summary failed")
            minutes = (
                f"# {active.title}\n\n_Summary unavailable ({exc}). The transcript is "
                "saved — run 'summarize the last meeting' to rebuild minutes._\n"
            )
        with open(active.paths.minutes_md, "w", encoding="utf-8") as f:
            f.write(minutes)
        if not self._keep_audio:
            import os

            for p in (active.paths.near_wav, active.paths.far_wav):
                if os.path.exists(p):
                    os.remove(p)
        self._write_manifest_for(active, "done")
        _log.info("meeting done id=%s", active.paths.id)

    def status(self) -> dict[str, object]:
        a = self._active
        if a is None:
            return {"active": False, "paused": False, "mic_only": False,
                    "elapsed_s": 0.0, "recorded_s": 0.0, "title": "", "state": "idle"}
        elapsed = (datetime.now() - a.started).total_seconds()
        return {
            "active": True, "paused": a.paused, "mic_only": a.mic_only,
            "elapsed_s": round(elapsed, 1), "recorded_s": round(a.near.recorded_s(), 1),
            "title": a.title, "state": "paused" if a.paused else "recording",
        }

    def finalize_interrupted(self) -> list[str]:
        """On startup, finalize any meeting left mid-flight from on-disk WAVs."""
        recovered: list[str] = []
        for meeting_id in self._store.find_interrupted():
            try:
                self._recover_one(meeting_id)
                recovered.append(meeting_id)
            except Exception:
                _log.exception("recovery failed id=%s", meeting_id)
        return recovered

    def _recover_one(self, meeting_id: str) -> None:
        paths = self._store._paths(meeting_id)  # noqa: SLF001 - store helper, intentional
        manifest = self._store.read_manifest(paths.dir)
        mic_only = bool(manifest.get("mic_only", False))
        import os

        repair_header(paths.near_wav) if os.path.exists(paths.near_wav) else None
        far = paths.far_wav if (not mic_only and os.path.exists(paths.far_wav)) else None
        if far:
            repair_header(far)
        transcript = self._transcriber.build(paths.near_wav, far, mic_only=mic_only)
        with open(paths.transcript_md, "w", encoding="utf-8") as f:
            f.write(transcript)
        minutes = self._summarizer.summarize(
            transcript, title=str(manifest.get("title", "Meeting")),
            date=str(manifest.get("started_at", ""))[:10], duration="recovered",
            mic_only=mic_only,
        )
        with open(paths.minutes_md, "w", encoding="utf-8") as f:
            f.write(minutes)
        data = dict(manifest)
        data["state"] = "done"
        self._store.write_manifest(paths, data)
        _log.info("meeting recovered id=%s", meeting_id)

    # --- manifest helpers ---
    def _write_manifest(self, state: str) -> None:
        assert self._active is not None
        self._write_manifest_for(self._active, state)

    def _write_manifest_for(self, active: _Active, state: str) -> None:
        self._store.write_manifest(
            active.paths,
            {
                "id": active.paths.id, "title": active.title,
                "started_at": active.started.isoformat(),
                "state": state, "mic_only": active.mic_only,
                "far_stream": {"status": "unavailable" if active.mic_only else "ok"},
                "pauses": active.pauses,
            },
        )


class _Active:
    """Mutable state for the one in-flight meeting."""

    def __init__(self, *, paths: MeetingPaths, title: str, started: datetime,
                 near: _StreamWriter, far: _StreamWriter | None, mic_only: bool) -> None:
        self.paths = paths
        self.title = title
        self.started = started
        self.near = near
        self.far = far
        self.mic_only = mic_only
        self.paused = False
        self.pauses: list[dict[str, str]] = []


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
```

Add a `recorded_s()` method to `_StreamWriter` (data bytes → seconds): `return self._writer.data_bytes / (16000 * 2)`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_meeting_recorder.py -v`
Expected: PASS. (Threads drain the small fake frame lists and stop cleanly.)

- [ ] **Step 5: Commit**

```bash
git add src/autobot/meeting/recorder.py tests/unit/test_meeting_recorder.py
git commit -m "feat(meeting): MeetingRecorder lifecycle + finalize + recovery"
```

---

## Task 13: `AUDIO_CAPTURE` permission

**Files:**
- Modify: `src/autobot/permissions.py`
- Test: `tests/unit/test_permissions_audio_capture.py`

**Interfaces:**
- Produces: `permissions.AUDIO_CAPTURE = "audio_capture"`; `status_of(AUDIO_CAPTURE)` returns `UNKNOWN` (no queryable native API); `snapshot([...AUDIO_CAPTURE])` includes it with label/why; `open_pane(AUDIO_CAPTURE)` opens a best-effort pane.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_permissions_audio_capture.py
from __future__ import annotations

from autobot import permissions


def test_audio_capture_status_unknown_by_default() -> None:
    assert permissions.status_of(permissions.AUDIO_CAPTURE) == permissions.UNKNOWN


def test_snapshot_includes_audio_capture() -> None:
    keys = [permissions.MICROPHONE, permissions.AUDIO_CAPTURE]
    snap = {row["key"]: row for row in permissions.snapshot(keys)}
    assert snap[permissions.AUDIO_CAPTURE]["label"] == "Audio Capture"
    assert snap[permissions.AUDIO_CAPTURE]["description"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_permissions_audio_capture.py -v`
Expected: FAIL — `AttributeError: module 'autobot.permissions' has no attribute 'AUDIO_CAPTURE'`.

- [ ] **Step 3: Add the permission**

Add the constant near the others (after `AUTOMATION`):

```python
AUDIO_CAPTURE = "audio_capture"
```

Add to `_PANE` (best effort — there is no stable dedicated anchor; Screen Recording's pane is the closest privacy surface and is acceptable as a landing spot):

```python
    AUDIO_CAPTURE: _PRIVACY + "ScreenCapture",
```

Add to `_LABEL` and `_WHY`:

```python
    AUDIO_CAPTURE: "Audio Capture",
```
```python
    AUDIO_CAPTURE: "Lets Jack capture the other participants' audio during a meeting.",
```

Do **not** add it to `_NATIVE` — there is no prompt-free status API, so `status_of` falls through to `_observed` and returns `UNKNOWN`, which the gate treats as "allowed to try" (design §3.1, §8). A runtime denial is handled reactively by the recorder's mic-only degradation.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_permissions_audio_capture.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/permissions.py tests/unit/test_permissions_audio_capture.py
git commit -m "feat(meeting): AUDIO_CAPTURE permission key"
```

---

## Task 14: meeting `Settings` fields

**Files:**
- Modify: `src/autobot/config.py` (in the `Settings` dataclass)
- Test: `tests/unit/test_config_meetings.py`

**Interfaces:**
- Produces: `allow_meetings: bool = False`, `meetings_dir: str = "~/.autobot/meetings"`, `meeting_keep_audio: bool = True`, `meeting_keep: int = 20`, `meeting_chunk_s: float = 30.0`, `meeting_overlap_s: float = 3.0`, `meeting_diarization: str = "dual_stream"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config_meetings.py
from __future__ import annotations

import json

from autobot.config import Settings


def test_meeting_defaults() -> None:
    s = Settings()
    assert s.allow_meetings is False
    assert s.meetings_dir == "~/.autobot/meetings"
    assert s.meeting_keep == 20 and s.meeting_keep_audio is True
    assert s.meeting_chunk_s == 30.0 and s.meeting_overlap_s == 3.0
    assert s.meeting_diarization == "dual_stream"


def test_meeting_overlay_from_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"allow_meetings": True, "meeting_keep": 5}))
    s = Settings.load(p)
    assert s.allow_meetings is True and s.meeting_keep == 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_config_meetings.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'allow_meetings'`.

- [ ] **Step 3: Add the fields** (in the `# --- capabilities ---` block, after `allow_notes`)

```python
    # --- meetings (opt-in; records both sides of a call, transcribes + summarizes) ---
    # Master capability flag (off by default, like allow_web). Registers the meeting
    # tools + the system-audio source + the mic tee.
    allow_meetings: bool = False
    meetings_dir: str = "~/.autobot/meetings"
    # Keep the WAVs after transcription (True) or delete them to save space (False).
    meeting_keep_audio: bool = True
    # Retain the most recent N meetings; older folders are pruned on stop.
    meeting_keep: int = 20
    # On-stop transcription windowing (memory bound only; not a real-time knob).
    meeting_chunk_s: float = 30.0
    meeting_overlap_s: float = 3.0
    # "dual_stream" (you/participants) today; a finer per-speaker mode can be added here.
    meeting_diarization: str = "dual_stream"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_config_meetings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/config.py tests/unit/test_config_meetings.py
git commit -m "feat(meeting): meeting Settings fields"
```

---

## Task 15: `tools/meeting.py` — gated tools

**Files:**
- Create: `src/autobot/tools/meeting.py`
- Test: `tests/unit/test_meeting_tools.py`

**Interfaces:**
- Consumes: `MeetingRecorder` (12), `ToolRegistry`/`ToolSpec` (registry), `Risk`, `permissions.MICROPHONE`.
- Produces: `MeetingTools(recorder)` with `.specs() -> list[ToolSpec]`; `register_meeting_tools(registry, recorder) -> MeetingTools`. Tools: `start_meeting(title?)` (WRITE, `requires=MICROPHONE`), `stop_meeting()` (WRITE), `pause_meeting()`/`resume_meeting()` (WRITE), `meeting_status()` (READ_ONLY), `list_meetings()` (READ_ONLY), `summarize_meeting(id?)` (WRITE). All handlers return strings and never raise.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_meeting_tools.py
from __future__ import annotations

from autobot import permissions
from autobot.core.types import Risk
from autobot.tools.meeting import MeetingTools


class _FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, title: str) -> str:
        self.calls.append(f"start:{title}")
        return "Recording the meeting."

    def stop(self) -> str:
        return "Saved the meeting minutes to /x."

    def status(self) -> dict:  # type: ignore[type-arg]
        return {"active": False}


def _specs():  # type: ignore[no-untyped-def]
    return {s.name: s for s in MeetingTools(_FakeRecorder()).specs()}


def test_start_requires_microphone_and_is_write() -> None:
    spec = _specs()["start_meeting"]
    assert spec.requires == permissions.MICROPHONE
    assert spec.risk == Risk.WRITE


def test_status_is_read_only() -> None:
    assert _specs()["meeting_status"].risk == Risk.READ_ONLY


def test_handlers_return_strings() -> None:
    rec = _FakeRecorder()
    tools = MeetingTools(rec)
    assert "recording" in tools.start_meeting(title="Standup").lower()
    assert rec.calls == ["start:Standup"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_tools.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `tools/meeting.py`**

```python
# src/autobot/tools/meeting.py
"""Gated tools that drive the meeting recorder (design §8)."""

from __future__ import annotations

from autobot import permissions
from autobot.core.types import Risk
from autobot.logging_setup import get_logger
from autobot.meeting.recorder import MeetingRecorder
from autobot.tools.registry import ToolRegistry, ToolSpec

_log = get_logger("meeting")


class MeetingTools:
    """Start/stop/pause/resume + status/list/summarize, backed by the recorder."""

    def __init__(self, recorder: MeetingRecorder) -> None:
        self._rec = recorder

    def start_meeting(self, title: str = "") -> str:
        """Begin recording the meeting."""
        return self._rec.start(title)

    def stop_meeting(self) -> str:
        """Stop, transcribe, summarize, and save the meeting."""
        return self._rec.stop()

    def pause_meeting(self) -> str:
        """Pause capture."""
        return self._rec.pause()

    def resume_meeting(self) -> str:
        """Resume capture."""
        return self._rec.resume()

    def meeting_status(self) -> str:
        """Report whether a meeting is recording and for how long."""
        st = self._rec.status()
        if not st["active"]:
            return "No meeting is recording right now."
        mins = int(float(st["elapsed_s"]) // 60)
        extra = " (paused)" if st["paused"] else (" — mic-only" if st["mic_only"] else "")
        return f"Recording “{st['title']}” for about {mins} min{extra}."

    def list_meetings(self) -> str:
        """List recent saved meetings."""
        recent = self._rec._store.list_recent()  # noqa: SLF001 - intentional helper use
        if not recent:
            return "You have no saved meetings yet."
        names = [f"“{m.get('title', m.get('id'))}” ({m.get('state')})" for m in recent[:10]]
        return "Recent meetings: " + "; ".join(names) + "."

    def summarize_meeting(self, id: str = "") -> str:  # noqa: A002 - tool arg name
        """Rebuild minutes for a saved meeting (the most recent if ``id`` omitted)."""
        return self._rec.resummarize(id or None)

    def specs(self) -> list[ToolSpec]:
        """Return the tool specs (relevance-gated; not core)."""
        return [
            ToolSpec(
                name="start_meeting",
                description=(
                    "Start recording the current meeting/call (Google Meet, Zoom, any "
                    "app) to take minutes. Captures both your microphone and the other "
                    "participants' audio, on-device. Cues: 'take minutes', 'record this "
                    "meeting', 'start recording the call'. Optional `title` names it."
                ),
                parameters={
                    "type": "object",
                    "properties": {"title": {"type": "string", "description": "Optional meeting name."}},
                    "required": [],
                },
                handler=self.start_meeting,
                risk=Risk.WRITE,
                requires=permissions.MICROPHONE,
                ack="Starting the recording.",
            ),
            ToolSpec(
                name="stop_meeting",
                description=(
                    "Stop the in-progress meeting recording, then transcribe and "
                    "summarize it. Cues: 'stop recording', 'end the meeting', 'finish "
                    "taking minutes', 'wrap up the call'."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.stop_meeting,
                risk=Risk.WRITE,
                ack="Wrapping up and writing the minutes.",
            ),
            ToolSpec(
                name="pause_meeting",
                description="Pause the meeting recording (e.g. for a private aside). Cue: 'pause recording'.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.pause_meeting,
                risk=Risk.WRITE,
                ack="Pausing.",
            ),
            ToolSpec(
                name="resume_meeting",
                description="Resume a paused meeting recording. Cue: 'resume recording'.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.resume_meeting,
                risk=Risk.WRITE,
                ack="Resuming.",
            ),
            ToolSpec(
                name="meeting_status",
                description="Say whether a meeting is being recorded and for how long. Cue: 'are you recording?'.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.meeting_status,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="list_meetings",
                description="List recent recorded meetings and their state. Cue: 'what meetings have you saved?'.",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.list_meetings,
                risk=Risk.READ_ONLY,
            ),
            ToolSpec(
                name="summarize_meeting",
                description=(
                    "Rebuild the minutes for a saved meeting from its transcript (the "
                    "most recent if no id given). Cue: 'summarize the last meeting again'."
                ),
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Optional meeting id/folder name."}},
                    "required": [],
                },
                handler=self.summarize_meeting,
                risk=Risk.WRITE,
                ack="Rebuilding the minutes.",
            ),
        ]


def register_meeting_tools(registry: ToolRegistry, recorder: MeetingRecorder) -> MeetingTools:
    """Register the meeting tools into ``registry``."""
    tools = MeetingTools(recorder)
    for spec in tools.specs():
        registry.register(spec)
    _log.info("meeting tools registered")
    return tools
```

Add a `resummarize(self, meeting_id: str | None) -> str` method to `MeetingRecorder` (Task 12) that loads the saved transcript (or recovers from WAVs), re-runs the summarizer, writes `minutes.md`, and returns a friendly string. Add a focused recorder test for it in `test_meeting_recorder.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/test_meeting_tools.py tests/unit/test_meeting_recorder.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autobot/tools/meeting.py src/autobot/meeting/recorder.py tests/unit/test_meeting_tools.py tests/unit/test_meeting_recorder.py
git commit -m "feat(meeting): gated meeting tools + resummarize"
```

---

## Task 16: Wire everything into `app.py::build()`

**Files:**
- Modify: `src/autobot/tts/voices.py` (add `ensure_syscap`) OR create `src/autobot/syscap.py`
- Modify: `src/autobot/app.py` (`build()`)
- Test: `tests/unit/test_build_meetings_smoke.py`

**Interfaces:**
- Consumes: everything above.
- Produces: when `allow_meetings` is True, `build()` seeds the sidecar, builds a `FrameTee` over the mic, builds a `MeetingRecorder` (near branch from the tee, far source = `CoreAudioTapSource` factory), wires the summarizer's completer to `language_model.complete`, registers the meeting tools, and calls `recorder.finalize_interrupted()`.

- [ ] **Step 1: Add `ensure_syscap` (model on `ensure_voice`)**

```python
# in src/autobot/tts/voices.py (or a new syscap.py)
def ensure_syscap(bundled_dir: str | None) -> str | None:
    """Locate the bundled ``autobot-syscap`` binary, seeding it on first run.

    The orb app passes ``AUTOBOT_SYSCAP_DIR`` pointing at its bundled binaries
    (Tauri extracts the target-triple-suffixed sidecar there). Returns the path to
    a runnable binary, or ``None`` if it isn't available (dev runs degrade to mic-only).
    """
    import os
    import shutil
    from pathlib import Path

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
```

- [ ] **Step 2: Wire into `build()`** (after the `allow_notes` block, ~line 449; the voice IO + STT are built ~lines 510-527)

Add near the STT build (so `stt` and the language model exist). Because the tee must own the mic and the turn loop must still get frames, build the recorder's near branch from the same tee the turn loop's audio source reads. Add:

```python
    if settings.allow_meetings:
        import os as _os

        from autobot.io.mic_tee import FrameTee
        from autobot.io.system_audio_mac import CoreAudioTapSource
        from autobot.meeting.recorder import MeetingRecorder
        from autobot.meeting.store import MeetingStore
        from autobot.meeting.summarizer import MeetingSummarizer
        from autobot.meeting.transcriber import MeetingTranscriber
        from autobot.tools.meeting import register_meeting_tools
        from autobot.tts.voices import ensure_syscap

        syscap_bin = ensure_syscap(_os.environ.get("AUTOBOT_SYSCAP_DIR"))

        # The mic tee is created lazily inside the recorder's near-branch factory so a
        # chat-only launch never opens the mic. The factory builds (or reuses) a tee
        # over the same mic source the turn loop uses, returning a fresh branch.
        def _near_branch() -> object:
            tee = _voice_io.frame_tee()  # see Step 3: LazyVoiceIO exposes the shared tee
            return tee.branch_started()

        def _far_source() -> CoreAudioTapSource:
            if not syscap_bin:
                raise RuntimeError("syscap binary not available")
            return CoreAudioTapSource(syscap_bin, exclude_pid=_os.getpid())

        store = MeetingStore(settings.meetings_dir)
        transcriber = MeetingTranscriber(
            stt, chunk_s=settings.meeting_chunk_s,
            overlap_s=settings.meeting_overlap_s, stt_prompt=settings.stt_prompt,
        )
        summarizer = MeetingSummarizer(
            lambda p: stt and language_model.complete(p),  # completer; see note below
            max_chars=_summary_window_chars(settings),
        )
        recorder = MeetingRecorder(
            store, transcriber, summarizer,
            near_branch_factory=_near_branch, far_source_factory=_far_source,
            keep_audio=settings.meeting_keep_audio, keep=settings.meeting_keep,
            on_event=(on_meeting_event if 'on_meeting_event' in dir() else None),
        )
        register_meeting_tools(registry, recorder)
        recorder.finalize_interrupted()
        log.info("meetings ENABLED dir=%s", settings.meetings_dir)
        print("[meeting] meetings ENABLED — Jack can record and summarize calls.")
```

> **Implementer notes (resolve while wiring, since `build()` constructs these locally):**
> - `language_model` is the value `build()` already constructs (the `OllamaLanguageModel`/cloud model via `_build_language_model`/`ReloadableLanguageModel`); use that exact local variable name. The completer is just `language_model.complete`.
> - `_summary_window_chars(settings)`: a small helper returning `max(2000, settings.context_tokens * 3)` (≈3 chars/token) when `context_tokens` is set, else a safe default like `8000`. Add it next to the other private builders in `app.py`.
> - The far-end `exclude_pid` is the **daemon's** PID (`os.getpid()`), the process that plays Jack's TTS — so his voice is excluded from `far.wav`.

- [ ] **Step 3: Expose the shared tee from `LazyVoiceIO`**

`io/lazy_voice.py` already builds mic + TTS + recorder lazily. Add a `frame_tee()` accessor that, on first call, wraps the built mic `FrameSource` in a `FrameTee`, rebuilds the turn-loop audio source to read one tee branch, starts the tee, and returns it; subsequent calls return the same tee. Add `branch_started()` to `FrameTee` = `b = self.branch(); self.start(); return b` (start is idempotent). Add a unit test in `test_mic_tee.py` for `branch_started` idempotency.

> This is the most integration-heavy step. If reworking `LazyVoiceIO` is risky, the fallback (documented in design §C) is: the recorder's near branch opens its **own** short-lived `MicFrameSource` only while a meeting is active, and the turn loop keeps its own — accepting that on macOS two input streams on the same device generally coexist. Prefer the tee; fall back only if hardware testing shows the tee destabilizes the turn loop.

- [ ] **Step 4: Add a smoke test**

```python
# tests/unit/test_build_meetings_smoke.py
from __future__ import annotations

from autobot.config import Settings


def test_build_registers_meeting_tools_when_enabled(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # allow_meetings on, everything else minimal; mic/voice stay lazy so no hardware.
    s = Settings(allow_meetings=True, meetings_dir=str(tmp_path), interaction_mode="chat",
                 allow_app_control=False, allow_system_info=False, allow_system_toggles=False,
                 allow_file_search=False, allow_clipboard=False, allow_reminders=False,
                 allow_notes=False, allow_memory=False, allow_file_io=False)
    orch = __import__("autobot.app", fromlist=["build"]).build(settings=s)
    # The meeting tools are registered (gated, so present in specs()).
    names = {spec.name for spec in orch._registry.specs()}  # noqa: SLF001
    assert {"start_meeting", "stop_meeting", "meeting_status"} <= names
```

(Adjust the orchestrator's registry accessor to match the real attribute; if `build()` requires Ollama reachable, monkeypatch `_build_language_model` to a fake exposing `complete`.)

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/unit/test_build_meetings_smoke.py -v && make check`

```bash
git add src/autobot/app.py src/autobot/tts/voices.py src/autobot/io/lazy_voice.py src/autobot/io/mic_tee.py tests/unit/test_build_meetings_smoke.py tests/unit/test_mic_tee.py
git commit -m "feat(meeting): wire recorder + tee + tools into the composition root"
```

---

## Task 17: daemon routes + `MeetingEvent`

**Files:**
- Modify: `src/autobot/core/events.py` (add `MeetingEvent` + `EventBus.publish_meeting`)
- Modify: `src/autobot/daemon/server.py` (routes + an `on_meeting` callback) and the daemon runner that calls `create_app`/`build`
- Test: `tests/unit/test_meeting_event.py`

**Interfaces:**
- Produces: `MeetingEvent(state, elapsed_s, recorded_s, mic_only, paused, title)` with `.message()` → `{"type": "meeting", ...}`; `EventBus.publish_meeting(status: dict)`; routes `POST /meeting/{start,stop,pause,resume}`, `GET /meeting/{status,list}` calling injected `on_meeting` callbacks wired to the recorder.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_meeting_event.py
from __future__ import annotations

from autobot.core.events import EventBus, MeetingEvent


def test_message_shape() -> None:
    msg = MeetingEvent(state="recording", elapsed_s=12.0, recorded_s=10.0,
                       mic_only=False, paused=False, title="Standup").message()
    assert msg["type"] == "meeting" and msg["state"] == "recording" and msg["title"] == "Standup"


def test_publish_meeting_fans_out() -> None:
    bus = EventBus()
    seen: list[dict] = []  # type: ignore[type-arg]
    bus.subscribe(seen.append)
    bus.publish_meeting({"state": "recording", "elapsed_s": 1.0, "recorded_s": 1.0,
                         "mic_only": False, "paused": False, "title": "x"})
    assert seen and seen[-1]["type"] == "meeting"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_meeting_event.py -v`
Expected: FAIL — no `MeetingEvent`.

- [ ] **Step 3: Add the event + publisher**

In `core/events.py`, add the dataclass (next to the other events):

```python
@dataclass(frozen=True, slots=True)
class MeetingEvent:
    """Meeting recording state, for the orb's record indicator + timer."""

    state: str           # idle | recording | paused | transcribing | summarizing | done
    elapsed_s: float
    recorded_s: float
    mic_only: bool
    paused: bool
    title: str

    def message(self) -> dict[str, object]:
        return {
            "type": "meeting", "state": self.state, "elapsed_s": self.elapsed_s,
            "recorded_s": self.recorded_s, "mic_only": self.mic_only,
            "paused": self.paused, "title": self.title,
        }
```

Add to `EventBus`:

```python
    def publish_meeting(self, status: dict[str, object]) -> None:
        """Broadcast a meeting status frame (the recorder's ``status()`` dict)."""
        self._emit(
            MeetingEvent(
                state=str(status.get("state", "idle")),
                elapsed_s=float(status.get("elapsed_s", 0.0)),
                recorded_s=float(status.get("recorded_s", 0.0)),
                mic_only=bool(status.get("mic_only", False)),
                paused=bool(status.get("paused", False)),
                title=str(status.get("title", "")),
            ).message()
        )
```

- [ ] **Step 4: Add routes** (in `create_app`, after the other routes; add an `on_meeting: Any | None = None` param — a `Callable[[str, dict], str]` dispatching `action -> recorder method`)

```python
    async def post_meeting(action: str, request: Request) -> dict[str, Any]:
        if on_meeting is None:
            return {"ok": False, "error": "meetings disabled"}
        payload = await request.json() if action == "start" else {}
        reply = await asyncio.to_thread(on_meeting, action, payload if isinstance(payload, dict) else {})
        return {"ok": True, "reply": reply}

    async def get_meeting_status() -> dict[str, Any]:
        return {"status": on_meeting("status", {})} if on_meeting else {"status": {"active": False}}

    app.add_api_route("/meeting/start", lambda r: post_meeting("start", r), methods=["POST"])
    app.add_api_route("/meeting/stop", lambda r: post_meeting("stop", r), methods=["POST"])
    app.add_api_route("/meeting/pause", lambda r: post_meeting("pause", r), methods=["POST"])
    app.add_api_route("/meeting/resume", lambda r: post_meeting("resume", r), methods=["POST"])
    app.add_api_route("/meeting/status", get_meeting_status, methods=["GET"])
    app.add_api_route("/meeting/list", lambda: {"meetings": on_meeting("list", {})} if on_meeting else {"meetings": []}, methods=["GET"])
```

In the daemon runner that builds the orchestrator + app: pass `on_meeting_event=bus.publish_meeting` into `build(...)`, and wire `on_meeting` in `create_app` to a small dispatcher that calls `recorder.start/stop/pause/resume/status` and `list_meetings` (route through the gate for the WRITE actions exactly as `on_action`/`run_tool` does today, so start/stop are audited and confirmed identically to a voice/chat trigger).

> **Implementer note:** add an `on_meeting_event` parameter to `build()` (mirroring `on_mcp_event`) and thread it into the `MeetingRecorder(on_event=...)` constructed in Task 16.

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/unit/test_meeting_event.py -v && make check`

```bash
git add src/autobot/core/events.py src/autobot/daemon/ src/autobot/app.py tests/unit/test_meeting_event.py
git commit -m "feat(meeting): daemon routes + MeetingEvent on the bus"
```

---

## Task 18: packaging — build, sign, and bundle the sidecar

**Files:**
- Modify: `Makefile`
- Modify: `packaging/autobot-daemon.spec`
- Modify: `ui/orb-shell/src-tauri/tauri.conf.json`
- Modify: `.github/workflows/` release workflow (sign step)

**Interfaces:**
- Produces: a signed `autobot-syscap-<target-triple>` placed in `ui/orb-shell/src-tauri/binaries/`, declared in Tauri `externalBin`, so the `.app` ships it and `ensure_syscap` (Task 16) finds it.

- [ ] **Step 1: Add Makefile targets**

```makefile
build-syscap: ## Build + sign the native system-audio sidecar (requires Xcode)
	cd autobot-syscap && swift build -c release
	codesign --force --options runtime \
	  --entitlements packaging/syscap.entitlements \
	  -s "$(CODESIGN_IDENTITY)" autobot-syscap/.build/release/autobot-syscap
	codesign -dv autobot-syscap/.build/release/autobot-syscap
```

In the existing `bundle` target, copy the sidecar next to the daemon before `cargo tauri build`:

```makefile
	cp autobot-syscap/.build/release/autobot-syscap "$(SIDECAR_DIR)/autobot-syscap-$(TARGET_TRIPLE)"
```

Add `packaging/syscap.entitlements` (`com.apple.security.device.audio-input` → true).

- [ ] **Step 2: Bundle in the daemon spec too** (so a frozen-daemon-only path still has it)

In `packaging/autobot-daemon.spec`, after the existing `datas`/`binaries` blocks:

```python
_syscap = os.path.join(_ROOT, "autobot-syscap", ".build", "release", "autobot-syscap")
if os.path.isfile(_syscap):
    binaries += [(_syscap, ".")]
```

- [ ] **Step 3: Declare in Tauri config**

```jsonc
// ui/orb-shell/src-tauri/tauri.conf.json — externalBin
"externalBin": [
  "binaries/autobot-daemon",
  "binaries/autobot-syscap"
]
```

Ensure the orb passes `AUTOBOT_SYSCAP_DIR` (pointing at the extracted binaries dir) into the daemon's environment alongside `AUTOBOT_VOICE_DIR`.

- [ ] **Step 4: Validate the packaged app on hardware**

```bash
make build-syscap CODESIGN_IDENTITY="Developer ID Application: <YOUR ID>"
make bundle
# Launch the bundled app, enable Meetings in Settings, join a Meet call, say
# "take minutes", confirm the Audio-Capture prompt fires and far.wav has remote voices.
```

Expected: prompt fires (signed), both `near.wav`/`far.wav` written, `transcript.md` + `minutes.md` produced. This is the end-to-end acceptance test.

- [ ] **Step 5: Commit**

```bash
git add Makefile packaging/ ui/orb-shell/src-tauri/tauri.conf.json .github/workflows/
git commit -m "build(meeting): build, sign, and bundle autobot-syscap sidecar"
```

---

## Task 19: design-reference doc cross-link + final `make check`

**Files:**
- Modify: `docs/plans/autobot_meeting_minutes_plan.md` (add a one-line "Implemented by" pointer to this plan)

- [ ] **Step 1:** Add to the design doc header: `> Implementation plan: docs/superpowers/plans/2026-07-01-meeting-minutes.md`.
- [ ] **Step 2:** Run the full suite: `make check`. Expected: ruff, ruff-format, mypy strict, and pytest all green.
- [ ] **Step 3:** Manual smoke per design §15 (enable → "take minutes" → talk → "stop recording" → open minutes), and the mic-only path (decline Audio Capture → still get a one-sided transcript).
- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs(meeting): link design reference to implementation plan"
```

---

## Self-Review (completed during authoring)

**Spec coverage** — every design section maps to a task: §3 capture → Task 0/5; §3.1 permission → Task 13; §3.2 process-exclude → Task 0/16; §4.1 protocols → Tasks 1–4, 5, 10; §4.2 modules → Tasks 5–12, 15; §5 pipeline/degradation/pause → Task 12; §5.5 recovery + WAV repair → Tasks 7, 8, 12; §6 English → Tasks 2/3/9; §7 minutes/labels → Tasks 9, 11; §8 tools/gate → Tasks 13, 15, 17; §9 storage → Task 8; §10 config → Task 14; §11 daemon/UI → Task 17; §12 edge cases → distributed (mic-only T12, double-start T12, stop-no-meeting T12, LLM-offline T12, recovery T8/12); §13 logging → every module uses `get_logger("meeting")`; §14 testing → each task is TDD; §16 de-risk-first → Task 0 is first.

**Placeholder scan** — no "TBD"/"handle errors"/"similar to". The two adaptation points (LLM `complete` client handles in Task 10; `LazyVoiceIO`/`build()` local names in Tasks 16–17) carry concrete reference code plus an explicit "match the existing handle" instruction, because those exact attribute names live in files this plan modifies but didn't quote in full.

**Type consistency** — `Segment(text,start,end)`, `MeetingPaths`, `transcribe_segments(...)`, `complete(prompt,*,temperature)`, `MeetingRecorder(... near_branch_factory, far_source_factory ...)`, `Completer`, and `MeetingEvent` fields are used identically across the tasks that produce and consume them.
