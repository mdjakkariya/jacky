"""The coder turn: plan → approve → act, driven over a suspend/resume channel.

A coding turn runs on a daemon worker thread that can *park awaiting an answer from
the CLI* and *resume* when it arrives. :class:`TurnChannel` is the two-queue handoff;
:class:`SuspendingConfirmer` is the coder gate's confirmer (it routes mid-act
confirmations to the active turn's channel instead of a TTY it doesn't have).
"""

from __future__ import annotations

import queue
from typing import Any

from autobot.core.types import Risk, ToolCall, ToolExecutor, ToolResult
from autobot.logging_setup import get_logger
from autobot.tools.code.command_policy import classify_command
from autobot.tools.permission import PermissionGate

_log = get_logger("coder")


class TurnChannel:
    """Two-queue handoff between a parked worker turn and the HTTP layer.

    The worker calls :meth:`ask`/:meth:`done` (producer side); the HTTP handlers call
    :meth:`poll`/:meth:`answer` (consumer side). Both directions block, so the worker's
    Python call stack *is* the turn's continuation — no state is serialized between
    requests.
    """

    def __init__(self) -> None:
        """Create empty out/in queues."""
        self._out: queue.Queue[dict[str, Any]] = queue.Queue()
        self._in: queue.Queue[dict[str, str]] = queue.Queue()

    def ask(self, event: dict[str, Any]) -> dict[str, str]:
        """Worker: surface ``event`` to the HTTP layer, block for the CLI's answer."""
        self._out.put(event)
        return self._in.get()

    def done(self, reply: str) -> None:
        """Worker: surface the final reply and end the turn."""
        self._out.put({"status": "done", "reply": reply})

    def poll(self) -> dict[str, Any]:
        """HTTP: block for the worker's next event (a plan, a pending ask, or done)."""
        return self._out.get()

    def answer(self, value: str, text: str = "") -> None:
        """HTTP: deliver the CLI's answer to the parked worker."""
        self._in.put({"value": value, "text": text})


class SuspendingConfirmer:
    """Coder gate confirmer: suspends the turn to ask the CLI (no TTY of its own).

    The active turn's channel is set by :class:`CoderTurnDriver` via :meth:`set_channel`
    before each act phase. Answers ``"yes"``/``"y"``/``"once"`` proceed; anything else
    (or no active channel) cancels — the gate then reports the action wasn't performed.
    """

    def __init__(self) -> None:
        """Start with no active channel (set per turn by the driver)."""
        self._channel: TurnChannel | None = None

    def set_channel(self, channel: TurnChannel | None) -> None:
        """Point this confirmer at the active turn's channel (or ``None`` between turns)."""
        self._channel = channel

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Ask the CLI via the active channel; ``True`` only on an affirmative answer."""
        if self._channel is None:
            return False  # no active turn to ask — refuse rather than block forever
        answer = self._channel.ask({"status": "pending", "kind": kind, "prompt": prompt})
        return answer.get("value", "").strip().lower() in {"yes", "y", "once"}

    def confirm_action(self, prompt: str, kind: str = "danger") -> str:
        """Tri-state confirm for the gate: ``"once"`` on yes, ``""`` (cancel) otherwise."""
        return "once" if self.confirm(prompt, kind) else ""

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        """Coder tools don't use choices — grant the least-privilege default."""
        return default


_PLAN_ONLY_MSG = (
    "Planning phase — not executed. Add this step to your todo list; you'll carry it "
    "out after the plan is approved."
)


def read_only_executor(gate: PermissionGate) -> ToolExecutor:
    """An executor for the plan phase: run read-only tools, refuse anything that writes."""

    def execute(call: ToolCall) -> ToolResult:
        risk = gate.risk_of(call.name)
        if risk is not None and risk >= Risk.WRITE:
            _log.info("plan-phase refused tool=%s", call.name)
            return ToolResult(name=call.name, content=_PLAN_ONLY_MSG, ok=False)
        return gate.execute(call)  # READ_ONLY runs; an unknown tool → gate reports it

    return execute


def act_executor(
    gate: PermissionGate,
    allowlist: list[str],
    blocklist: list[str],
    *,
    ask_on_confirm: bool = True,
) -> ToolExecutor:
    """An executor for the act phase: auto-apply edits; classify run_command by policy.

    ``run_command`` is classified by :func:`classify_command`: ``"block"`` is refused
    outright; ``"allow"`` (user allowlist) runs pre-authorized (no prompt); ``"confirm"``
    falls through to the gate, which asks the CLI via the :class:`SuspendingConfirmer` —
    unless ``ask_on_confirm`` is ``False`` (``auto`` mode), where it runs pre-authorized
    too. Everything else (reads, in-cwd edits) goes straight to the gate; being below the
    gate's destructive threshold, edits never prompt.
    """

    def execute(call: ToolCall) -> ToolResult:
        if call.name == "run_command":
            command = str(call.arguments.get("command", ""))
            decision, reason = classify_command(command, allowlist, blocklist)
            if decision == "block":
                _log.info("command blocked cmd=%s", command)
                return ToolResult(
                    name=call.name,
                    content=f"That command is blocked for safety ({reason}).",
                    ok=False,
                )
            if decision == "allow" or (decision == "confirm" and not ask_on_confirm):
                _log.info("command auto-run cmd=%s", command)
                return gate.execute(call, pre_authorized=True)
            _log.info("command ask cmd=%s", command)  # confirm + ask → gate asks the CLI
        return gate.execute(call)

    return execute
