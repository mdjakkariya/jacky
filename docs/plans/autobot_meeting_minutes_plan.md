# Meeting minutes — record, transcribe & summarize a meeting, on-device (design)

Design reference for a new feature (its own GitHub issue + branch). Records *how*
Jack records a meeting, produces an English transcript, and writes minutes — and
*why* it's built this way. Status/tracking lives in the issue, not here.

> **Implementation plan:** [`docs/superpowers/plans/2026-07-01-meeting-minutes.md`](../superpowers/plans/2026-07-01-meeting-minutes.md).
> The on-device Python subsystem (Tasks 1–17) is implemented on `feat/meeting-minutes`.
> The native `autobot-syscap` sidecar (Task 0) and packaging/signing (Task 18) require
> Xcode + a Developer ID and are validated on real hardware — see the plan.

> Goal in one line: when the user asks ("Jack, take minutes of this meeting"),
> Jack captures **both sides** of a Google Meet / Zoom / any call, transcribes it
> to **English** entirely on-device, and saves a clean transcript plus structured
> minutes (summary, decisions, action items) into a **user-owned folder** that the
> user can then manage by voice/chat — reliably, for a call of **any length**.

This is a production design, not a staged prototype. Every piece below is the
shape we ship. This revision is grounded in the **actual** codebase (verified
against `core/interfaces.py`, `stt/`, `io/`, `tools/`, `permissions.py`,
`app.py`, `daemon/`) and the **real** macOS audio-capture permission model, and
folds in the product decisions taken during design review:

1. **Transcribe on stop, not live.** During the meeting Jack only writes audio to
   disk; all STT/summarization happens once, on `stop`. This deletes the live
   real-time path — the single largest source of flakiness — while the saved
   artifact stays complete. (§5)
2. **Graceful far-end degradation.** If system-audio capture is unavailable
   (permission, signing, OS), Jack records the user's side via the mic, warns
   clearly, and still produces a transcript. The feature never hard-fails. (§5, §8)
3. **Pause / resume** is in scope. (§5)
4. **Speaker labels are `[you]` / `[participants]` for v1**; per-person
   diarization is a reserved, documented seam, not v1 scope. The minutes still
   attribute owners by *name when the conversation names them*. (§7)

---

## 1. Context — why this is a new subsystem, not a new tool

Jack's entire pipeline is **turn-based**: `AudioSource.record_clip()` returns
*one utterance* (a 1-D `float32` mono 16 kHz `AudioClip`), `SpeechToText.transcribe()`
turns it into a `Transcription`, the `Orchestrator` runs one turn, done (see
`core/interfaces.py`, `orchestrator/state_machine.py`). A meeting is the
opposite shape — a **long, continuous, two-sided audio stream**.

So meeting capture is **not** a new `AudioSource` plugged into the turn loop. It
is a parallel subsystem (`meeting/`) that gated tools start and stop, runs on its
own threads, writes audio to disk, and — only on stop — reuses the existing
speech (`stt/`) and language (`llm/`) stages behind their protocols. The turn
loop is untouched.

Three hard problems sit inside it, each with a settled answer below:

1. **Capturing the far end.** In a Meet/Zoom call the remote participants' voices
   come out of the **speakers**, never into the mic. We capture **system audio
   output** with Core Audio process taps via a small signed native sidecar. (§3)
2. **Sharing the one microphone.** The turn loop needs the mic (so you can say
   "Jack, stop recording") *and* the meeting needs the mic for the near end. Two
   opens of one device race. We solve this with a single-owner **frame tee**. (§4)
3. **Any-hour reliability.** The design holds audio on **disk** and transcribes
   once on stop, so a 3-hour meeting costs the same RAM as a 3-minute one and
   survives a crash. (§5)

---

## 2. Non-negotiables carried in

- **On-device only** (constraint #1). No audio, transcript, or summary leaves the
  machine. The system-audio sidecar, STT, and summarization all run locally. The
  optional cloud LLM remains the user's existing opt-in choice for the *summary*
  step only, under the already-disclosed `llm_provider="anthropic"` path — never
  the audio. Default is local.
- **English only, enforced** (constraint #2). STT is pinned to English decode
  (`language="en"`); we never use Whisper's `translate` task and never auto-detect.
  Non-English speech is not "translated" — it is simply not produced as another
  language. (§6)
- **The permission gate is not optional** (constraint #3). Starting/stopping/
  pausing a recording and writing minutes are real actions; they flow through the
  registry + `PermissionGate` (risk classification, macOS-permission preflight,
  audit). (§8)
- **Engine stays headless** (constraint #4). All capture/transcription/summary
  logic lives in the Python daemon; the orb only renders a recording state and
  forwards "start/stop/pause/resume" intent.

---

## 3. Capturing system audio — Core Audio process taps via a native sidecar

**Decision: capture system output with Core Audio *process taps* (macOS 14.2+,
`CATapDescription` + an aggregate device), driven by a small bundled native
helper that streams raw PCM on stdout.** The dev/target machine is macOS 15, so
the API is always present.

Why this and not the alternatives:

- **ScreenCaptureKit audio (rejected).** The PyObjC binding is unreliable for
  **audio-only** capture on macOS 15 — streams die with `-3805
  connectionInvalid`, or the audio sample-buffer callback is never invoked
  (pyobjc #647).
- **Virtual audio device (BlackHole etc.) (rejected).** Requires the user to
  install a driver and re-route output. Off-mission for a zero-friction product.
- **Core Audio taps (chosen).** Taps intercept system output **without disrupting
  playback**, with **no virtual driver** — the tap is a sub-tap of an aggregate
  device whose main sub-device is the real output. This is the modern, supported,
  no-driver path.

### 3.1 The permission model (corrected)

This is the piece an earlier draft got wrong, so it is stated precisely:

- **Core Audio process taps require `NSAudioCaptureUsageDescription` — a
  *dedicated* TCC category ("Audio Capture"), distinct from both Microphone and
  Screen Recording.** It is **not** Screen Recording. The privacy indicator is the
  audio-recording dot, not a screen-recording indicator.
- A full meeting therefore needs **two** independent consents: **Microphone**
  (near end) and **Audio Capture** (far end). They are separate prompts.
- **The sidecar binary must be code-signed** (Developer ID) with the entitlement
  and an Info.plist `NSAudioCaptureUsageDescription` string, and built against
  deployment target **≥ 14.4** — *or the Audio-Capture prompt never fires*
  ("unsigned builds compile fine but can't exercise audio capture"). This is a
  hard CI/release gate (§14), not an afterthought.
- There is **no clean public API to query Audio-Capture status ahead of time**
  (AudioCap resorts to TCC probing). So our permission preflight reports `UNKNOWN`
  for it and we rely on the first-use system prompt plus reactive handling — which
  is exactly the existing `permissions.status_of` graceful-degradation contract.

### 3.2 Integration shape — a sidecar binary, not PyObjC

The taps API is Swift-first and the robust, well-trodden pattern is a tiny native
CLI that opens the tap and writes **raw PCM to stdout, diagnostics to stderr**.
Jack spawns it as a subprocess and reads frames off the pipe — the *same*
subprocess pattern already used for `osascript` in `tools/notes.py`
(`subprocess.run(..., capture_output=True, check=False, timeout=…)`, args as
argv, never spliced), and the same lazy-import + graceful-fallback discipline as
`io/aec_mac.py`. Reference implementations to model the helper on: Apple's
`AudioCap` sample and `AudioTee` (Core Audio taps → fixed PCM chunks on stdout,
configurable sample rate, mono).

We ship our **own** small Swift helper, **`autobot-syscap`**, bundled in the
`.app` via Tauri's `externalBin` with the target-triple suffix — exactly like the
existing `autobot-daemon` sidecar — and seeded/located the way voices are
(`tts/voices.py::ensure_voice`), so there is no runtime third-party dependency and
we control the output contract.

**Output contract:** little-endian 16-bit PCM, **mono, 16 kHz** (resampled inside
the helper), framed to match the rest of the pipeline; metadata/diagnostics on
stderr.

**Exclude Jack's own audio.** `CATapDescription` supports process exclusion. The
tap is created as "system output **excluding** Jack's own audio-producing
process(es)" so that Jack's spoken acks and TTS answers — and any sound the app
itself makes — **never leak into `far.wav`**. (Other apps' audio, e.g. music or
notifications, *is* system output and *will* be captured; this is documented
behavior, and the user should mute unrelated audio. Per-app exclusion is a later
refinement, not v1.)

### 3.3 Two independent streams

- **Near end** = the microphone, reusing the existing `FrameSource`
  (`io/listening.py::MicFrameSource`, or `io/aec_mac.py::VoiceProcessingMicSource`
  when AEC is on). This is the local speaker(s). AEC, when enabled, *helps* here —
  it keeps the remote voices (coming from the speakers) out of the near channel,
  so the two streams stay cleanly separated.
- **Far end** = the system-audio tap (the sidecar above). This is everyone remote.

Keeping them separate gives **two-channel labeling for free** — "you" vs. "the
call" — with no diarization model (§7).

---

## 4. New protocol & components

Following `CLAUDE.md`'s "how to add a component" recipe; `app.py::build()` stays
the only place that names concretes. Heavy runtimes are lazy-imported.

### 4.1 Protocols (`core/interfaces.py`)

**System-audio source** (new):

```python
class SystemAudioSource(Protocol):
    """Continuous far-end (system output) capture for meetings."""
    def frames(self) -> Iterator[AudioClip]: ...   # 16 kHz mono float32, like FrameSource
    def close(self) -> None: ...
    aec_active: bool                                # parity w/ mic source flags (False here)
```

**Segment-level STT** (new, *additive*). The conversational
`SpeechToText.transcribe(audio) -> Transcription` returns only `{text,
confidence}` with **no timestamps** — it cannot drive a two-stream merge. Rather
than overload it (and disturb the turn loop), we add one method to the protocol:

```python
@dataclass(frozen=True, slots=True)
class Segment:            # new value object in core/types.py
    text: str
    start: float          # seconds from stream start
    end: float

class SpeechToText(Protocol):
    def transcribe(self, audio: AudioClip) -> Transcription: ...          # unchanged (turn loop)
    def transcribe_segments(                                              # new (meetings)
        self,
        audio: AudioClip,
        *,
        language: str = "en",
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: str | None = None,
    ) -> list[Segment]: ...
```

- **faster-whisper** implements it natively (`segments` already carry `.start`/
  `.end`; `word_timestamps` stays optional/best-effort — segment-level timing is
  sufficient to interleave and dedupe).
- **whisper.cpp** implements it from its per-segment start/end times (with the
  same graceful `TypeError` fallback already used for `initial_prompt`).
- **`ReloadableSTT`** forwards `transcribe_segments` exactly as it forwards
  `transcribe` (same lock, same lazy build), so meetings get hot-swappable STT for
  free, including the `whisper_cpp` Metal engine for faster on-stop transcription.

### 4.2 Concretes / modules

- `io/system_audio_mac.py` — `CoreAudioTapSource` implementing `SystemAudioSource`
  by spawning and reading `autobot-syscap`. Lazy-imported; **any** failure (old
  OS, permission denied, helper missing/unsigned, pipe EOF) raises a typed error
  so the caller degrades to mic-only cleanly rather than crashing. Carries the
  same "manually validated on real hardware" caveat as `io/aec_mac.py`.
- `io/mic_tee.py` — `FrameTee`: a single owner pulls the persistent mic
  `FrameSource` and fans **copies** of each frame to N bounded subscriber queues.
  The turn loop reads one branch; the meeting near-end writer reads another. One
  device owner, no double-open races, no frame-stealing. Pure enough to unit-test
  with a fake source. (When no meeting is active, there is one subscriber and the
  behavior is identical to today.)
- `meeting/recorder.py` — `MeetingRecorder`: owns the two capture→disk writer
  threads, the pause/resume gate, and the stop→finalize sequence. Sources + STT +
  summarizer are **injected**, so it is unit-tested with fakes (mirrors how
  `WakeWordVadRecorder` is tested without hardware).
- `meeting/store.py` — `MeetingStore`: creates/locates the per-meeting folder,
  the incremental WAV writers (valid header maintained / repaired on recovery),
  reads/writes the manifest, enforces retention, and finds interrupted meetings on
  startup.
- `meeting/transcriber.py` — `MeetingTranscriber`: pure chunk-windowing +
  overlap-dedupe + two-stream interleave-by-timestamp + speaker tagging. Fully
  unit-tested against synthetic segment lists.
- `meeting/summarizer.py` — `MeetingSummarizer`: the map-reduce minutes builder
  (pure planning logic + an injected `LanguageModel` call).

---

## 5. Capture & finalization pipeline — built for unbounded length

### 5.1 During the meeting: disk only (no STT, no LLM)

Both streams are written, frame by frame, to their own 16 kHz mono WAV under the
meeting folder (`near.wav`, `far.wav`) via an incremental writer. **No
transcription runs while recording.** RAM holds only the current frame buffer.
16 kHz mono int16 is ~1.9 MB/min/stream (~115 MB/hour/stream) — negligible, and
audio is deletable after transcription (`meeting_keep_audio`). This phase is, by
construction, almost impossible to make flaky: it is just two append-to-file
loops fed by queues.

The near-end writer reads its branch of the `FrameTee` (§4); the far-end writer
reads `CoreAudioTapSource`. Each stream's wall-clock `started_at` is recorded in
the manifest so the two segment timelines can be aligned to a common `t0` at
finalization (small ms skew is irrelevant to readability).

### 5.2 Pause / resume

`pause_meeting` stops appending to **both** WAVs and records `{at}` in the
manifest's `pauses` list; `resume_meeting` resumes and records `resumed_at`.
Because paused spans are simply not written, each WAV stays **contiguous audio**
— transcription is unaffected. `elapsed` (wall-clock) and `recorded` (audio)
durations are tracked separately; the transcript can annotate `[paused ~Ns]` from
the `pauses` list. Pause/resume are idempotent (double-pause → "already paused").

### 5.3 On stop: the single authoritative pass

`stop_meeting` flips state `recording → transcribing`, then:

1. **Transcribe each WAV** through `ReloadableSTT.transcribe_segments` with the
   long-form-safe settings: `language="en"`, `vad_filter=True` (kills Whisper
   silence hallucinations), `condition_on_previous_text=False` (one bad window
   can't poison later ones), `initial_prompt=stt_prompt` (names/jargon biasing).
   Long WAVs are read in **~30 s windows with ~3 s overlap** purely to bound
   memory; the overlap region is **deduped by timestamp**. Because there is no
   real-time pressure, this pass is the **authoritative** one — there is no second
   pass to reconcile.
2. **Merge** near + far segment lists into one chronological `transcript.md`, each
   line tagged `[you]` / `[participants]`.
3. Flip `transcribing → summarizing`, build `minutes.md` (§7), then `→ done`.

On the M2 target, `small.en` on CPU transcribes an hour of two streams in minutes,
not real time — fine, because it runs after the meeting. Users wanting a faster
finalize set `stt_engine="whisper_cpp"` (Metal) — a one-line config change, not new
code.

### 5.4 Mic-only degradation (far end unavailable)

On `start_meeting`, Jack attempts to start `CoreAudioTapSource`. **If it fails**
(Audio-Capture denied, unsigned/dev build, helper missing, OS too old), Jack:
logs the reason, sets `manifest.far_stream = "unavailable"` (+ reason) and
`mic_only = true`, opens the Audio-Capture settings pane **once**, speaks *"I can
only hear your side — the others' audio isn't being captured,"* and **records
near-end only**. A one-sided transcript (`[you]` only) is still produced. The
feature never hard-fails on the far end.

### 5.5 Manifest & crash recovery

`manifest.json` records:

```json
{ "id", "title", "started_at", "ended_at", "state", "mic_only",
  "far_stream": {"status": "ok|unavailable|interrupted", "reason"},
  "streams": {"near": {"started_at"}, "far": {"started_at"}},
  "pauses": [{"at", "resumed_at"}], "settings" }
```

`state ∈ {recording, paused, transcribing, summarizing, done, interrupted}`. On
daemon startup, any meeting left in `recording`/`paused`/`transcribing`/
`summarizing` is detected and **finalized from the WAVs on disk** (transcribe →
summarize → done) — nothing is silently lost. The incremental WAV writer keeps a
valid RIFF header (or the recovery step repairs the header from the file's byte
length) so a hard crash mid-write still yields a transcribable file.

---

## 6. English enforcement (constraint #2)

Enforced at the decode call, not assumed from the model:

- `language="en"` is passed on every `transcribe_segments` call, so a stray
  non-English utterance can't flip the decoder into another language.
- `task` stays `transcribe`, never `translate` — we transcribe English; we do not
  translate other-language audio *into* English (that would violate the "English
  in/out" contract).
- The `.en` model default (`small.en`) is English-only by construction.
- The existing `stt_prompt` initial-prompt biasing (names, jargon) carries over
  via `initial_prompt`.

---

## 7. Minutes, speaker labels & differentiation

### 7.1 Hierarchical map-reduce summarization

An hour transcript overflows the default local model's context window, so the
summary is **map-reduce**:

1. **Segment** the transcript into chunks sized to the live model's window
   (auto-detected via `context_tokens` / `anthropic_context_tokens`).
2. **Map**: summarize each chunk → per-chunk notes (decisions, actions, points).
3. **Reduce**: combine into the final minutes; if combined notes still overflow,
   group and reduce again (recurse) until one structured result remains.

Output (`minutes.md`) is **structured, deterministic** (temperature 0): title +
date + duration (recorded vs. elapsed if paused), attendee sides, a prose summary,
**Decisions**, **Action items**, and **Open questions**. The summary call reuses
the existing `LanguageModel` (local Ollama by default; the user's opt-in cloud
choice applies, audio never sent) via a dedicated non-conversational method (not
`run_turn`). If the LLM is unavailable at stop time, the **transcript is still
saved** and `minutes.md` notes "Summary unavailable (model offline) — rebuild with
`summarize_meeting`."

### 7.2 Speaker labels: what's possible, and what isn't

Worth stating plainly, because it bounds expectations:

- **You vs. the call — solved, free, stable.** The two-stream design yields
  `[you]` / `[participants]` with no model and no flakiness.
- **Individual remote participants — not recoverable "for free."** Meet/Zoom
  **mix all remote participants into one downlink** before it reaches your
  speakers. The tap captures that mix, so Alice/Bob/Carol are a single waveform in
  `far.wav`. The same is true of two people sharing your mic (both are `[you]`).

Telling individual speakers apart requires **speaker diarization** — a separate
model that clusters audio by voice. It produces **anonymous** labels
("Speaker 1/2"), not names (Meet/Zoom expose no per-speaker metadata to an audio
tap), and it is the **least reliable** component in any meeting tool (overlap,
cross-talk, short utterances). It therefore conflicts with the stability goal and
is **out of v1 scope**.

### 7.3 Named owners without diarization (the pragmatic win)

The minutes already attribute **Action items** and **Decisions** to real people
**when the conversation names them** ("Alice, can you own the deploy?" → *owner:
Alice*) — the LLM extracts this from the *words*, not the voices, at zero
acoustic-separation risk. For most meetings this captures the part people actually
want ("who's doing what").

### 7.4 Diarization as a reserved seam (future v2)

The design keeps diarization a clean, additive extension, not a rewrite:

- The `Segment`/transcript schema reserves a `speaker` field.
- The `meeting_diarization` config key selects the mode (`"dual_stream"` today).
- When added, the right fit for **this** codebase is an **ONNX/CPU** diarizer
  (segmentation + speaker-embedding + clustering, sherpa-onnx-style) — **not**
  torch/pyannote — consistent with the repo's no-torch posture (it already runs
  Silero VAD as vendored ONNX). Diarization segments would be aligned to the
  Whisper segments by timestamp (the WhisperX pattern), refining the
  `[participants]` label into `Speaker N` on the far stream only.

---

## 8. Tools, permissions & the gate (constraint #3)

`tools/meeting.py` registers (all through the registry + `PermissionGate`,
relevance-gated like other non-core tools; each method returns a string and never
raises, per the tool convention):

| Tool | Risk | `requires` | Notes |
|------|------|-----------|-------|
| `start_meeting(title?)` | `WRITE` | `MICROPHONE` | Attempts far-end tap; degrades to mic-only on failure (§5.4). Ack "Recording the meeting." Refuses if one is already recording. |
| `stop_meeting()` | `WRITE` | — | Stops capture, runs transcribe + summarize, writes files. "No meeting is recording" if none. |
| `pause_meeting()` / `resume_meeting()` | `WRITE` | — | Idempotent; friendly no-ops when not applicable. |
| `meeting_status()` | `READ_ONLY` | — | Recording? mic-only? elapsed / recorded time, paused. |
| `summarize_meeting(id?)` | `WRITE` | — | (Re)build minutes from a saved transcript (or from WAVs if no transcript). |
| `list_meetings()` | `READ_ONLY` | — | Recent meetings + folders + state. |

Deleting/moving recordings needs **no new tool** — the existing `files` / `fileio`
/ `trash` tools manage the meeting folder through the same gate
(destructive-confirm on delete), since `meetings_dir` is a granted root (§9).

**Why `requires=MICROPHONE` only.** The mic is the one **mandatory** permission
(without it there is no near end and nothing to record) *and* the one that can be
**preflighted** (`permissions.status_of(MICROPHONE)`), so the gate refuses-and-
opens-Settings before the tool runs — never a deep native failure. The Audio-
Capture permission **cannot** be preflighted (§3.1), so it is handled reactively:
attempt the tap, and on denial open its pane + warn + degrade. This also sidesteps
the fact that `ToolSpec.requires` is a single string — there is exactly one
preflightable hard gate.

**`permissions.py` addition.** Add an `AUDIO_CAPTURE = "audio_capture"` constant
with a best-effort pane URL and label/why text. Its native status check returns
`UNKNOWN` (no queryable API), which the existing `status_of` contract already
handles by allowing the attempt. (Microphone is already present and unchanged.)

**Visible by design (the "silent" model the user asked for).** Jack does the
*work* silently — capture, transcribe, summarize, file — but the recording itself
is opt-in and visible: macOS shows the mic indicator (near end) and the audio-
recording indicator (far-end tap) whenever capture is live, unbypassable by OS
policy. For a privacy-first product this is correct; consent for recording others
is the user's responsibility. The whole feature is behind `allow_meetings` (off by
default).

---

## 9. Storage layout — a folder the user owns

```
~/.autobot/meetings/2026-06-30-1015-standup/
  manifest.json        # id, title, times, state, mic_only, far_stream, pauses, settings
  near.wav  far.wav    # raw audio (far.wav absent if mic_only; deletable per meeting_keep_audio)
  transcript.md        # timestamped, speaker-tagged ([you] / [participants])
  minutes.md           # summary · decisions · action items · open questions
```

`meetings_dir` defaults under `~/.autobot/` and is a granted root in the
`AccessPolicy`, so Jack can list/read/move/delete via the normal file tools, and
the user can open the folder directly in Finder. Retention is bounded
(`meeting_keep`, mirroring `session_keep`); the active meeting is never pruned.

---

## 10. Config (single source — `config.py`)

New `Settings` fields (all with defaults; persisted via the Settings view; no env
vars; following the `allow_web`/`allow_notes` capability-flag pattern):

- `allow_meetings: bool = False` — master capability flag (off by default).
  Registers the meeting tools + system-audio source + the mic tee.
- `meetings_dir: str = "~/.autobot/meetings"`
- `meeting_keep_audio: bool = True` — keep WAVs after transcription, or delete.
- `meeting_keep: int = 20` — retain the most recent N meetings.
- `meeting_chunk_s: float = 30.0`, `meeting_overlap_s: float = 3.0` — **on-stop**
  transcription windowing (memory bound only; no longer a real-time knob).
- `meeting_diarization: str = "dual_stream"` — `"dual_stream"` (near/far) today; a
  finer per-speaker option can be added behind this key (§7.4).

---

## 11. Daemon & UI surface

- Endpoints (alongside existing FastAPI routes in `daemon/server.py`):
  `POST /meeting/start`, `POST /meeting/stop`, `POST /meeting/pause`,
  `POST /meeting/resume`, `GET /meeting/status`, `GET /meeting/list`.
- A new `MeetingEvent` on the existing event bus (`core/events.py`,
  `EventBus.publish_meeting(...)`, wired through `app.build()` like the other
  `on_*` callbacks) carries `{state, elapsed_s, recorded_s, far_stream, mic_only,
  paused, title}`. The orb shows a record indicator + timer (and a paused/mic-only
  badge); the chat drawer offers "Open minutes" when `done`. State is re-published
  on subscribe and reconciled from the manifest after a daemon restart, so the UI
  is correct across reconnects.
- Trigger by voice or chat ("Jack, take minutes of this meeting" / "stop
  recording") — handled entirely by the gated tools above; no change to the turn
  loop or wake gate (the tee makes "stop recording" audible mid-meeting).

---

## 12. Edge cases & failure modes (handle every one)

The capture phase is deliberately trivial; almost all risk is at the seams. Each
of these has a defined, tested behavior:

| Situation | Behavior |
|-----------|----------|
| `allow_meetings` off | Meeting tools not registered; not advertised. |
| Mic permission missing | Gate refuses `start_meeting`, opens Mic pane (preflight). |
| Audio-Capture denied / unsigned dev build / OS < 14.4 / helper missing | Degrade to mic-only, open Audio-Capture pane once, warn, record near end (§5.4). |
| Sidecar crashes / pipe EOF mid-meeting | Mark `far_stream=interrupted@T`, keep near end, warn; meeting continues. |
| Mic device change/unplug mid-meeting | Catch the stream error; attempt reopen, else mark near interrupted and continue with whatever streams remain; never crash. |
| Disk full while writing | Stop writers, mark `interrupted`, preserve bytes already flushed, warn. |
| Jack speaks / TTS during meeting | Excluded from `far.wav` via tap process-exclusion (§3.2). |
| Other-app audio (music, notifications) | Captured (it's system output); documented — user should mute. |
| Start while already recording | Refuse: "A meeting is already recording." (single-active invariant) |
| Stop / pause with no active meeting | Friendly no-op message. |
| Immediate start→stop / silence-only meeting | Empty transcript handled; minutes say "No speech detected." |
| Transcript overflows model context | Map-reduce recursion (§7.1). |
| LLM offline at stop | Transcript saved; minutes note "Summary unavailable — rebuild with `summarize_meeting`." |
| Cloud provider chosen but offline / no key | Same graceful path; transcript always saved. |
| Daemon crash mid recording/transcribing/summarizing | Finalized from on-disk WAVs on next startup (§5.5). |
| Partial/headerless WAV after hard crash | Header repaired from byte length during recovery. |
| Non-English speech | Not translated; likely VAD-skipped or low-confidence (English-only contract, §6). |
| Retention prune | Oldest beyond `meeting_keep` removed; active meeting never pruned. |

---

## 13. Logging (per `CLAUDE.md`)

A `meeting` component logger (`get_logger("meeting")`, tag `[meeting]`), seam
events only (never per-frame): `start` (title, streams, mic_only, settings),
periodic `captured seconds=… frames=… stream=near|far`, `pause` / `resume`,
`stop`, `transcribe stream=… windows=… chars=…`, `merge segments=…`,
`summarize map chunks=…` / `reduce`, the written file paths, and the
degradation/interruption reasons. Errors with traceback via `_log.exception`.
Greppable via `make logs-grep C=meeting`.

---

## 14. Testing (fast, hardware-free)

Pure logic is fully unit-tested; native capture and the live mic/tap are excluded
from unit tests (consistent with the rest of the suite):

- `transcriber`: chunk windowing + overlap dedupe, near/far interleave by
  timestamp, speaker tagging, **mic-only single-stream** path — synthetic
  segment lists.
- `summarizer`: map-reduce planning (chunk sizing, recursion on overflow),
  LLM-offline path — against a fake `LanguageModel`.
- `store`: folder/slug creation, manifest read/write, WAV header repair,
  retention pruning, crash recovery (detect interrupted → finalize), pause-list
  handling — against a temp dir.
- `recorder`: start/stop/pause/resume lifecycle, disk-write behavior,
  **far-end-failure degradation**, sidecar-EOF handling — with **fake**
  `FrameSource` / `SystemAudioSource` / STT (no audio hardware, no model).
- `mic_tee`: fan-out correctness, subscriber add/remove, no frame loss or
  stealing across branches — fake frame source.
- `tools/meeting`: risk levels, `requires=MICROPHONE` wiring, gate refusal when
  the mic permission is reported missing, single-active-meeting refusal,
  degradation messaging.
- `permissions`: `AUDIO_CAPTURE` key returns `UNKNOWN` gracefully and never
  blocks the attempt.

The native `autobot-syscap` helper and `CoreAudioTapSource` are validated
**manually on real hardware** (same explicit caveat carried on `io/aec_mac.py`) —
the one piece that cannot be proven green in CI. Its release path additionally
requires **code signing + entitlement + Info.plist usage string** verified, or the
Audio-Capture prompt won't fire (§3.1).

---

## 15. Getting started — "just enable and use it"

The zero-friction path the design targets:

1. In Settings, flip **Meetings** on (`allow_meetings`).
2. Say "Jack, take minutes of this meeting." First use prompts for **Microphone**
   (usually already granted) and **Audio Capture** (one system prompt — fires
   because the bundled sidecar is signed).
3. Talk. Say "Jack, stop recording." Jack transcribes + summarizes and offers
   "Open minutes."

If Audio Capture is declined, step 2 still records your side and tells you how to
enable the other side next time — it never dead-ends.

---

## 16. Build order — de-risk the one real unknown first

Everything except system-audio capture is assembly of pieces Jack already has
(Whisper behind `SpeechToText`, the LLM behind `LanguageModel`, the file tools,
the gate, the permission preflight, the subprocess pattern, sidecar bundling). The
single genuine unknown is the **Core Audio tap helper on real, signed hardware** —
aggregate-device routing, the Audio-Capture prompt flow, and own-process
exclusion. So `autobot-syscap` is built, **signed**, and validated end-to-end
first (capture 30 s of Meet audio → clean `far.wav`; confirm the prompt fires and
Jack's own audio is excluded) **before** the surrounding subsystem is wired.

---

## References

- Core Audio taps — Apple: <https://developer.apple.com/documentation/CoreAudio/capturing-system-audio-with-core-audio-taps>
- AudioCap (Swift sample, taps): <https://github.com/insidegui/AudioCap>
- AudioTee (taps → PCM on stdout): <https://github.com/makeusabrew/audiotee>
- Capturing system audio on macOS in 2026 (TCC `NSAudioCaptureUsageDescription`, signing, deployment target): <https://dgrlabs.co/blog/2026-04-25-capturing-system-audio-on-macos-in-2026.html>
- AudioTee write-up (output contract, mono, chunking): <https://stronglytyped.uk/articles/audiotee-capture-system-audio-output-macos>
- PyObjC ScreenCaptureKit audio-only bug: <https://github.com/ronaldoussoren/pyobjc/issues/647>
- faster-whisper: <https://github.com/SYSTRAN/faster-whisper>
- Whisper long-form best practices: <https://www.saytowords.com/blogs/Whisper-Best-Settings/>
- WhisperX (VAD cut & merge, timestamp alignment, diarization pattern): <https://arxiv.org/pdf/2303.00747>
- Map-reduce summarization: <https://medium.com/@sonimegha1602/scaling-document-summarization-with-llms-stuffing-map-reduce-and-refine-a8a468d479c3>
- CPU speaker diarization (later option): <https://picovoice.ai/blog/state-of-speaker-diarization/>
