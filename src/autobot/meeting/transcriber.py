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
    """Format seconds as HH:MM:SS timestamp."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(lines: list[tuple[str, Segment]], *, mic_only: bool) -> str:
    """Render the merged lines to markdown.

    Args:
        lines: List of (speaker_tag, segment) tuples.
        mic_only: If True, add note that mic-only audio was recorded.

    Returns:
        Markdown string with transcript.
    """
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
        """Initialize transcriber with STT engine and windowing parameters.

        Args:
            stt: Speech-to-text engine implementing SpeechToText protocol.
            chunk_s: Window size in seconds.
            overlap_s: Overlap size in seconds.
            stt_prompt: Initial prompt to condition the STT model.
        """
        self._stt = stt
        self._chunk_s = chunk_s
        self._overlap_s = overlap_s
        self._prompt = stt_prompt

    def transcribe_stream(self, wav_path: str) -> list[Segment]:
        """Transcribe one WAV, windowed to bound memory, deduping the overlaps.

        Args:
            wav_path: Path to WAV file to transcribe.

        Returns:
            List of deduplicated segments with corrected timestamps.
        """
        audio = read_wav(wav_path)
        total_s = len(audio) / _SAMPLE_RATE
        collected: list[Segment] = []
        windows_count = 0
        for start_s, end_s in plan_windows(total_s, self._chunk_s, self._overlap_s):
            windows_count += 1
            window = audio[int(start_s * _SAMPLE_RATE) : int(end_s * _SAMPLE_RATE)]
            for seg in self._stt.transcribe_segments(
                window,
                language="en",
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=self._prompt or None,
            ):
                collected.append(Segment(seg.text, seg.start + start_s, seg.end + start_s))
        deduped = dedupe_overlap(collected)
        _log.info(
            "transcribe stream=%s windows=%d segments=%d",
            wav_path,
            windows_count,
            len(deduped),
        )
        return deduped

    def build(self, near_wav: str, far_wav: str | None, *, mic_only: bool) -> str:
        """Transcribe both streams (or just near) and render the merged transcript.

        Args:
            near_wav: Path to near (microphone) WAV file.
            far_wav: Path to far (participant) WAV file, or None for mic-only.
            mic_only: If True, only transcribe near_wav.

        Returns:
            Markdown transcript with speaker tags.
        """
        near = self.transcribe_stream(near_wav)
        far = self.transcribe_stream(far_wav) if far_wav and not mic_only else []
        lines = merge_streams(near, far)
        return render_transcript(lines, mic_only=mic_only)
