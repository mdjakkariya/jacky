"""Pure composer for the live region: the spinner line and an optional activity line.

No terminal I/O and no threads — it only turns state (verb, frame char, elapsed, current
activity) into prompt_toolkit ``(style, text)`` fragments. The running Application paints
these; that single owner is what removes the threaded-``Live`` write race (rich#1530).
"""

from __future__ import annotations

# Jack's own verb pool — professional, with a light workshop motif fitting the name.
VERBS: tuple[str, ...] = (
    "Working",
    "Thinking",
    "Planning",
    "Tracing",
    "Reading",
    "Wiring",
    "Drafting",
    "Assembling",
    "Untangling",
    "Fitting",
    "Tightening",
    "Leveling",
    "Bracing",
    "Hoisting",
    "Rigging",
)


def verb_for(turn_index: int) -> str:
    """Pick a verb by turn index — varied across turns, deterministic (no RNG)."""
    return VERBS[turn_index % len(VERBS)]


def byline(elapsed_s: float, width: int) -> str:
    """A width-gated ``esc to interrupt · Ns`` byline (drops parts as width shrinks)."""
    secs = f"{int(elapsed_s)}s"
    full = f"esc to interrupt · {secs}"
    if width >= len(full) + 4:
        return full
    if width >= len(secs) + 4:
        return secs
    return ""


def live_fragments(
    verb: str, frame_char: str, elapsed_s: float, activity: str, width: int
) -> list[tuple[str, str]]:
    """Compose the live region as prompt_toolkit ``(style, text)`` fragments.

    Line 1 is ``<glyph> <verb>…  ·  <byline>``. If ``activity`` is non-empty, a second dim
    line shows the current tool activity below it. Styles are prompt_toolkit class names
    (resolved by the app's ``Style`` map), not rich styles.
    """
    frags: list[tuple[str, str]] = [
        ("", " "),  # left margin — aligns the spinner with the transcript's 1-col margin
        ("class:spinner", frame_char + " "),
        ("class:verb", f"{verb}…"),
    ]
    tail = byline(elapsed_s, width)
    if tail:
        frags.append(("class:dim", f"  ·  {tail}"))
    if activity:
        frags.append(("", "\n "))  # newline + left margin
        frags.append(("class:dim", activity))
    return frags
