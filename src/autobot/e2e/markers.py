"""Predicates over the `pyte`-rendered screen text — the harness's sync vocabulary.

Derived from the CLI's glyphs/cards (`autobot.cli.theme` / `cli.render`), so they track
the real TUI: ``⏺`` reply, ``⎿`` tool line, the ``Proceed?`` gate cards, and the ``❯``
idle prompt. All are pure ``str -> bool`` so they unit-test against canned screens.
"""

from __future__ import annotations

from collections.abc import Callable

from autobot.cli import theme

Marker = Callable[[str], bool]


def reply_present(screen: str) -> bool:
    """The assistant reply gutter (``⏺``) is on screen."""
    return theme.GLYPH_ASSISTANT in screen


def tool_line(screen: str) -> bool:
    """A nested tool-activity line (``⎿``) is on screen."""
    return theme.GLYPH_TOOL in screen


def plan_card(screen: str) -> bool:
    """The plan-approval card (``Proceed?`` + an ``Edit`` option)."""
    return "Proceed?" in screen and "Edit" in screen


def permission_card(screen: str) -> bool:
    """The command-permission card (``Proceed?`` + ``run it``)."""
    return "Proceed?" in screen and "run it" in screen


def any_gate(screen: str) -> bool:
    """Either interactive gate (plan or permission) is awaiting an answer."""
    return plan_card(screen) or permission_card(screen)


def working(screen: str) -> bool:
    """A turn is actively running — the spinner byline (``esc to interrupt``) is up."""
    return "esc to interrupt" in screen


def turn_started(screen: str) -> bool:
    """The turn has visibly begun: spinner, a tool line, or a gate is on screen."""
    return working(screen) or tool_line(screen) or any_gate(screen)


def error(screen: str) -> bool:
    """An error segment (``Error:``) is on screen."""
    return "Error:" in screen


def idle_prompt(screen: str) -> bool:
    """The REPL prompt is the last line AND *empty* — a turn finished, ready for input.

    Requires nothing after the prompt glyph. A prompt still showing un-submitted input
    (``❯ do a thing``) is **not** idle — otherwise a just-typed command, sitting in the
    input during the latency before its turn produces output, would be mistaken for a
    completed turn and the harness would race ahead / tear down mid-turn.
    """
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].lstrip()
    return last.startswith(theme.GLYPH_PROMPT) and not last[len(theme.GLYPH_PROMPT) :].strip()


BY_NAME: dict[str, Marker] = {
    "reply_present": reply_present,
    "tool_line": tool_line,
    "plan_card": plan_card,
    "permission_card": permission_card,
    "any_gate": any_gate,
    "working": working,
    "turn_started": turn_started,
    "error": error,
    "idle_prompt": idle_prompt,
}
