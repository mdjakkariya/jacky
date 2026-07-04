"""The coder turn: plan → approve → act, driven over a suspend/resume channel.

A coding turn runs on a daemon worker thread that can *park awaiting an answer from
the CLI* and *resume* when it arrives. :class:`TurnChannel` is the two-queue handoff;
:class:`SuspendingConfirmer` is the coder gate's confirmer (it routes mid-act
confirmations to the active turn's channel instead of a TTY it doesn't have).
"""

from __future__ import annotations

import queue
import re
import threading
from collections.abc import Callable
from typing import Any

from autobot.config import Settings
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
        self.closed = False

    def ask(self, event: dict[str, Any]) -> dict[str, str]:
        r"""Worker: surface ``event`` to the HTTP layer, block for the CLI's answer.

        If the channel has been :meth:`close`\ d (this turn was reclaimed by a fresh
        ``start()``), return a reject immediately without enqueuing to the out queue —
        a stale worker must self-decline and never surface events on a dead channel.
        """
        if self.closed:
            return {"value": "reject", "text": ""}
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

    def close(self) -> None:
        """Mark the channel closed and unblock a parked ``ask`` with a reject.

        Used to reclaim a turn parked awaiting an answer (the CLI died or a fresh
        ``start()`` superseded it): the parked worker thread wakes up, sees a reject,
        and its own ``_run`` unwinds normally via its ``channel.done(...)`` — it never
        touches the new turn's channel.
        """
        self.closed = True
        self._in.put({"value": "reject", "text": ""})


class SuspendingConfirmer:
    """Coder gate confirmer: suspends the turn to ask the CLI (no TTY of its own).

    The active turn's channel is set by :class:`CoderTurnDriver` via :meth:`set_channel`,
    called from the WORKER thread at the top of its run — so each worker thread has its
    own channel in thread-local storage. This makes confirm routing impossible to
    cross-wire between turns: even if two turns' lifetimes briefly overlap (a reclaim
    racing a stale worker), each thread's ``confirm`` can only ever see the channel that
    thread itself set. Answers ``"yes"``/``"y"``/``"once"`` proceed; anything else (or no
    active channel) cancels — the gate then reports the action wasn't performed.
    """

    def __init__(self) -> None:
        """Start with no active channel on any thread (set per turn by the driver)."""
        self._local = threading.local()

    def set_channel(self, channel: TurnChannel | None) -> None:
        """Point the CALLING thread's confirmer at ``channel`` (or ``None`` between turns)."""
        self._local.channel = channel

    def _channel_for_thread(self) -> TurnChannel | None:
        """The calling thread's active channel, if any."""
        channel: TurnChannel | None = getattr(self._local, "channel", None)
        return channel

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        """Ask the CLI via this thread's channel; ``True`` only on an affirmative answer."""
        channel = self._channel_for_thread()
        if channel is None:
            return False  # no active turn to ask — refuse rather than block forever
        answer = channel.ask({"status": "pending", "kind": kind, "prompt": prompt})
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
    unless ``ask_on_confirm`` is ``False``, where it runs pre-authorized too.
    ``ask_on_confirm`` is ``True`` only in ``confirm`` mode; in ``plan`` mode the plan was
    already approved and in ``auto`` mode nothing prompts. Everything else (reads, in-cwd
    edits) goes straight to the gate; being below the gate's destructive threshold, edits
    never prompt.
    """

    def execute(call: ToolCall) -> ToolResult:
        if call.name == "run_command":
            command = str(call.arguments.get("command", ""))
            logged_command = command[:200]  # cap: a long/newline-laden command is noise
            decision, reason = classify_command(command, allowlist, blocklist)
            if decision == "block":
                _log.info("command blocked cmd=%s", logged_command)
                return ToolResult(
                    name=call.name,
                    content=f"That command is blocked for safety ({reason}).",
                    ok=False,
                )
            if decision == "allow" or (decision == "confirm" and not ask_on_confirm):
                _log.info("command auto-run cmd=%s", logged_command)
                return gate.execute(call, pre_authorized=True)
            _log.info("command ask cmd=%s", logged_command)  # confirm + ask → gate asks CLI
        return gate.execute(call)

    return execute


_PLAN_PROMPT_PREFIX = (
    "You are a coding agent in PLANNING mode. First decide whether the request needs "
    "changes to files or commands to run.\n"
    "- If it does NOT (a greeting, small talk, a question you can answer, or a request "
    "too vague to act on): just reply normally — answer it, or ask for the detail you "
    "need. Do NOT write a numbered plan.\n"
    "- If it DOES: use ONLY read-only tools (read_file, grep, glob, repo_map) to explore, "
    "then reply with a concise NUMBERED todo list of the edits and commands you will make. "
    "Do not edit files or run commands yet.\n\nRequest: "
)
_ACT_PROMPT = (
    "Your plan is approved. Carry it out now, step by step: make the edits and run the "
    "commands from your plan, then briefly report what you did."
)
_CANCELLED_REPLY = "Okay, I won't make any changes."
_ERROR_REPLY = "Something went wrong on my end, so I stopped. Nothing was changed."

_TODO_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.*\S)\s*$")


def _extract_todo(reply: str) -> list[str]:
    """Best-effort: pull numbered/bulleted step text from a plan reply (for future UIs)."""
    return [m.group(1) for line in reply.splitlines() if (m := _TODO_LINE.match(line))]


class CoderTurnDriver:
    """Runs one coder turn (plan → approve → act) on a worker thread over a TurnChannel.

    ``start`` spawns the worker and returns its first event; ``reply`` delivers the CLI's
    answer and returns the next event. Only one turn runs at a time (guarded by a lock):
    a second ``start`` while a turn is actively running is rejected, while a turn is
    *parked* awaiting an answer (a CLI that died) is reclaimed.
    """

    def __init__(
        self,
        llm: Any,
        gate: PermissionGate,
        confirmer: SuspendingConfirmer,
        settings_provider: Callable[[], Settings],
    ) -> None:
        """Wire the driver. ``llm`` must expose ``run_turn(text, execute)``."""
        self._llm = llm
        self._gate = gate
        self._confirmer = confirmer
        self._settings = settings_provider
        self._lock = threading.Lock()
        self._channel: TurnChannel | None = None
        self._awaiting = False  # True when the worker is parked awaiting an answer

    def start(self, text: str) -> dict[str, Any]:
        """Begin a coder turn; return its first event (plan, pending, done, or error)."""
        with self._lock:
            if self._channel is not None and not self._awaiting:
                return {"status": "error", "reply": "A coding turn is already running."}
            if self._channel is not None and self._awaiting:
                # Reclaim a stale parked turn (CLI died or a fresh start superseded it):
                # close() unblocks the parked ask() with a self-decline so that worker's
                # _run unwinds via its own channel.done(...) and never touches this new
                # turn's channel.
                self._channel.close()
            channel = TurnChannel()
            self._channel = channel
            self._awaiting = False
            worker = threading.Thread(
                target=self._run, args=(channel, text), name="coder-turn", daemon=True
            )
            worker.start()
        return self._collect(channel)

    def reply(self, value: str, text: str = "") -> dict[str, Any]:
        """Deliver the CLI's answer to the parked turn; return the next event."""
        with self._lock:
            channel = self._channel
            if channel is None or not self._awaiting:
                return {"status": "error", "reply": "No coding turn is awaiting a reply."}
            self._awaiting = False
        channel.answer(value, text)
        return self._collect(channel)

    def _collect(self, channel: TurnChannel) -> dict[str, Any]:
        """Poll the channel for the next event, updating turn state."""
        event = channel.poll()
        with self._lock:
            if event.get("status") == "done":
                self._channel = None
                self._awaiting = False
            else:
                self._awaiting = True
        return event

    def _run(self, channel: TurnChannel, text: str) -> None:
        """Worker body: drive plan→approve→act per the autonomy dial. Never raises.

        Sets the confirmer's channel first thing, on THIS (worker) thread — the
        confirmer keys its active channel by thread-local, so this must run on the
        same thread that will later call ``gate.execute`` (and thus ``confirm``)
        during the act phase.
        """
        self._confirmer.set_channel(channel)
        try:
            autonomy = self._settings().coding_autonomy
            if autonomy == "plan":
                outcome, payload = self._plan_loop(channel, text)
                if outcome == "cancel":
                    channel.done(_CANCELLED_REPLY)
                    return
                if outcome == "reply":  # conversational / no actionable plan — just answer
                    channel.done(payload)
                    return
                reply = self._act()  # outcome == "act": session already holds the plan
            else:
                reply = self._act(first_text=text)  # confirm/auto: act on the request
            channel.done(reply)
        except Exception:  # a turn must always terminate with a reply for the CLI
            _log.exception("coder turn failed")
            channel.done(_ERROR_REPLY)

    def _plan_loop(self, channel: TurnChannel, text: str) -> tuple[str, str]:
        """Plan (read-only), then decide the next step.

        Returns one of:
            ``("reply", text)`` — the turn was conversational (a greeting, a question the
                coder answered from read-only tools, or a request for clarification): there
                is nothing to approve, so answer directly without a plan-approval gate.
            ``("act", "")`` — the user approved an actionable plan; run the act phase.
            ``("cancel", "")`` — the user rejected the plan.

        An actionable plan is detected by the presence of numbered/bulleted todo steps
        (:func:`_extract_todo`); a reply with none is treated as conversational.
        """
        request = text
        while True:
            executor = read_only_executor(self._gate)
            reply = self._llm.run_turn(_PLAN_PROMPT_PREFIX + request, executor)
            todo = _extract_todo(reply)
            if not todo:
                # No actionable steps — don't force an approve/act gate on a non-task.
                _log.info("plan: no actionable steps, answering directly")
                return "reply", reply
            _log.info("plan proposed steps=%d", len(todo))
            answer = channel.ask({"status": "plan", "reply": reply, "todo": todo})
            value = answer.get("value", "").strip().lower()
            if value in {"approve", "yes", "y"}:
                _log.info("plan approved")
                return "act", ""
            if value in {"reject", "no", "n"}:
                _log.info("plan rejected")
                return "cancel", ""
            request = answer.get("text") or text  # refine: re-plan with the feedback
            _log.info("plan refined")

    def _act(self, *, first_text: str | None = None) -> str:
        """Run the act phase with the executor tuned by the dial.

        Only ``confirm`` mode asks before each non-allowlisted command. In ``plan`` mode
        the user already approved the whole plan, so its commands run without a second
        prompt; ``auto`` runs everything. All three still refuse blocklisted commands and
        stay within the cwd jail + start-of-turn checkpoint.
        """
        settings = self._settings()
        ask_on_confirm = settings.coding_autonomy == "confirm"
        executor = act_executor(
            self._gate,
            settings.command_allowlist,
            settings.command_blocklist,
            ask_on_confirm=ask_on_confirm,
        )
        prompt = first_text if first_text is not None else _ACT_PROMPT
        reply: str = self._llm.run_turn(prompt, executor)
        _log.info("turn done")
        return reply
