"""Pure composer for the live region: a single spinner line describing the current action.

No terminal I/O and no threads — it only turns state (the current-action label, frame char,
elapsed) into prompt_toolkit ``(style, text)`` fragments. The running Application paints
these; that single owner is what removes the threaded-``Live`` write race (rich#1530).
"""

from __future__ import annotations

# Raw tool name → a short present-continuous label shown as the spinner's current action.
# Grouped by intent (not one entry per tool) so the spinner stays legible, not a tool dump.
_ACTIONS: dict[str, str] = {
    "read_file": "Reading file",
    "grep": "Searching",
    "glob": "Listing files",
    "list_dir": "Listing files",
    "run_command": "Running command",
    "write_file": "Editing file",
    "edit_file": "Editing file",
    "multi_edit": "Editing file",
    "spawn_agent": "Running subagent",
    "repo_map": "Mapping the repo",
    "update_plan": "Planning",
}

# The label shown while the model is thinking (no tool running yet).
DEFAULT_ACTION = "Working"


def action_label(name: str) -> str:
    """Map a raw tool name to a short current-action label (``read_file`` → ``Reading file``).

    Falls back to a humanized form of the name (``repo_stats`` → ``Repo stats``) so an unmapped
    tool still reads sensibly rather than showing a raw identifier.
    """
    known = _ACTIONS.get(name)
    if known:
        return known
    return name.replace("_", " ").strip().capitalize() or DEFAULT_ACTION


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
    label: str, frame_char: str, elapsed_s: float, width: int
) -> list[tuple[str, str]]:
    """Compose the live region as a single line of prompt_toolkit ``(style, text)`` fragments.

    The line is ``<glyph> <label>…  ·  <byline>`` — the label is the current action
    (``Reading file``, ``Running command``, …). No secondary preview line: the region stays a
    single, calm status line. Styles are prompt_toolkit class names (resolved by the app's
    ``Style`` map), not rich styles.
    """
    frags: list[tuple[str, str]] = [
        ("", " "),  # left margin — aligns the spinner with the transcript's 1-col margin
        ("class:spinner", frame_char + " "),
        ("class:verb", f"{label}…"),
    ]
    tail = byline(elapsed_s, width)
    if tail:
        frags.append(("class:dim", f"  ·  {tail}"))
    return frags
