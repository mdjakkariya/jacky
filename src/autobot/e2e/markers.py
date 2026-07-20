"""Predicates over the `pyte`-rendered screen text — the harness's sync vocabulary.

Derived from the CLI's glyphs/prompts (`autobot.cli.theme` / `cli.app`), so they track the
real TUI: ``⏺`` reply, ``⎿`` tool line, the live gate hints (permission ``Approve? [y]es ·
[n]o``; plan ``[y]es · [n]o · [e]dit``), and the ``❯`` idle prompt. All are pure ``str ->
bool`` so they unit-test against canned screens — but a canned screen is only as good as its
fidelity to the real render, so keep them copied from actual frames (see ``begin_modal`` in
``cli/app.py``).
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.cli import theme

Marker = Callable[[str], bool]


def reply_present(screen: str) -> bool:
    """The assistant reply gutter (``⏺``) is on screen."""
    return theme.GLYPH_ASSISTANT in screen


# Leading verbs of the nested activity lines (rendered as dim, indented verbs — no glyph).
_ACTIVITY_VERBS = ("Read ", "Listed ", "Searched ", "Edited ", "Ran ", "Mapped ", "Spawned ")


def tool_line(screen: str) -> bool:
    """A nested tool-activity line (a dim, indented verb like ``Read …`` or ``$ …``) is present."""
    return "^O to view" in screen or any(verb in screen for verb in _ACTIVITY_VERBS)


def plan_card(screen: str) -> bool:
    """The plan-approval gate is live — its ``[e]dit`` affordance is unique to a plan gate.

    The live plan hint is ``[y]es · [n]o · [e]dit``; a permission gate is ``Approve? [y]es ·
    [n]o`` (no edit). So ``[e]dit`` distinguishes the two and is present only while the plan
    modal is up — the committed plan reply is a plain ``⏺`` block with no choices line.
    """
    return "[e]dit" in screen


def permission_card(screen: str) -> bool:
    """The command-permission gate is live (``Approve?`` with no plan ``[e]dit`` affordance)."""
    return "approve?" in screen.lower() and "[e]dit" not in screen


def any_gate(screen: str) -> bool:
    """Either interactive gate (plan or permission) is awaiting an answer."""
    return plan_card(screen) or permission_card(screen)


def working(screen: str) -> bool:
    """A turn is actively running — a spinner frame is up in the live region.

    Keyed on the braille spinner glyph, NOT ``esc to interrupt`` — that hint now lives in the
    always-present status bar, so it can't distinguish a running turn from idle.
    """
    return any(frame in screen for frame in theme.SPINNER_FRAMES)


def turn_started(screen: str) -> bool:
    """The turn has visibly begun: spinner, a tool line, or a gate is on screen."""
    return working(screen) or tool_line(screen) or any_gate(screen)


def awaiting_reply(screen: str) -> bool:
    """A gate is *live* — its affordance is on screen in the live region, awaiting a choice.

    The gate affordance is shown in the transient live region only while awaiting an answer
    and vanishes the instant it's answered, so its mere presence IS the "awaiting now" signal.
    """
    return any_gate(screen)


def cost_view(screen: str) -> bool:
    """The ``/cost`` usage summary is on screen (its per-window ``Today`` + ``All time`` rows)."""
    return "Today" in screen and "All time" in screen


def error(screen: str) -> bool:
    """An error segment (``Error:``) is on screen."""
    return "Error:" in screen


def idle_prompt(screen: str) -> bool:
    """The input prompt is *empty* and nothing is running — a turn finished, ready for input.

    The docked input renders as ``❯`` with nothing typed (the status bar sits below it). So
    idle = an empty ``❯`` line is present AND no turn (spinner) or gate is active. A prompt
    still showing un-submitted input (``❯ do a thing``) is **not** idle — otherwise a
    just-typed command awaiting its turn's first output would look like a completed turn.
    """
    if working(screen) or any_gate(screen):
        return False
    # The input is drawn inside a Frame, so the prompt line carries border cells around the
    # glyph. Strip those (the box-drawing bar) before comparing, so an empty framed prompt
    # still reads as idle while a prompt holding un-submitted text does not.
    return any(ln.replace("│", "").strip() == theme.GLYPH_PROMPT for ln in screen.splitlines())


BY_NAME: dict[str, Marker] = {
    "reply_present": reply_present,
    "tool_line": tool_line,
    "plan_card": plan_card,
    "permission_card": permission_card,
    "any_gate": any_gate,
    "awaiting_reply": awaiting_reply,
    "working": working,
    "turn_started": turn_started,
    "cost_view": cost_view,
    "error": error,
    "idle_prompt": idle_prompt,
}
