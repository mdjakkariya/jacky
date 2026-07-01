"""Hierarchical map-reduce minutes from a (possibly very long) transcript (design §7.1)."""

from __future__ import annotations

from collections.abc import Callable

from autobot.logging_setup import get_logger

_log = get_logger("meeting")

Completer = Callable[[str], str]

# Hard cap so a model that never shrinks its input can't recurse forever.
_MAX_REDUCE_ROUNDS = 4

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
    """Split text on line boundaries into chunks no larger than ``max_chars``.

    A single line longer than ``max_chars`` becomes its own (oversized) chunk.
    """
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


def batch_notes(notes: list[str], max_chars: int) -> list[str]:
    """Group whole notes into batches whose joined length fits ``max_chars``.

    A note is never split; a single note larger than ``max_chars`` becomes its own
    batch. Each returned batch is one reduce input.
    """
    batches: list[str] = []
    current: list[str] = []
    size = 0
    for note in notes:
        extra = len(note) + (2 if current else 0)
        if current and size + extra > max_chars:
            batches.append("\n\n".join(current))
            current, size = [], 0
            extra = len(note)
        current.append(note)
        size += extra
    if current:
        batches.append("\n\n".join(current))
    return batches


class MeetingSummarizer:
    """Builds structured minutes via map-reduce over the transcript."""

    def __init__(self, complete: Completer, *, max_chars: int) -> None:
        self._complete = complete
        self._max_chars = max(1, max_chars)

    def _reduce(self, notes: list[str], _round: int = 0) -> str:
        """Combine notes, recursing while they overflow one window.

        Terminates unconditionally: it reduces whole-note batches (so the note
        count strictly falls when any batch groups >1 note), stops after
        ``_MAX_REDUCE_ROUNDS``, and bails to a truncated join if a round makes no
        structural progress (every note already exceeds the window). This keeps a
        model that never shrinks its input from looping forever.
        """
        combined = "\n\n".join(notes)
        if len(notes) <= 1 or len(combined) <= self._max_chars:
            return combined
        if _round >= _MAX_REDUCE_ROUNDS:
            _log.debug(
                "summarize reduce round-cap hit round=%d notes=%d",
                _round,
                len(notes),
            )
            return combined[: self._max_chars]
        batches = batch_notes(notes, self._max_chars)
        reduced = [self._complete(_REDUCE_PROMPT.format(notes=b)) for b in batches]
        if len(reduced) >= len(notes):
            # No structural progress (each note already oversized) — stop.
            _log.debug(
                "summarize reduce no-progress notes=%d batches=%d — truncating",
                len(notes),
                len(reduced),
            )
            return combined[: self._max_chars]
        _log.info("summarize reduce round=%d notes=%d->%d", _round, len(notes), len(reduced))
        return self._reduce(reduced, _round + 1)

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
