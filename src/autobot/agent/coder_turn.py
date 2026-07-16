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
from collections.abc import Callable, Iterator
from typing import Any

from autobot.agent.plan_state import PlanState
from autobot.config import Settings
from autobot.core.streaming import plan_sink
from autobot.core.types import Risk, ToolCall, ToolExecutor, ToolResult
from autobot.logging_setup import get_logger
from autobot.tools.code.command_policy import classify_command, is_read_only_command
from autobot.tools.permission import PermissionGate

_log = get_logger("coder")


class _TurnBusyError(Exception):
    """Raised internally when a fresh turn starts while one is actively running."""


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

    def emit(self, event: dict[str, Any]) -> None:
        """Worker: push a non-terminal streaming event (token/tool) to the HTTP layer."""
        if not self.closed:
            self._out.put(event)

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
    before_mutation: Callable[[], None] | None = None,
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

    ``before_mutation`` is fired exactly once, just before the first tool that actually
    changes the workspace runs — a file-mutating edit (``Risk.WRITE`` and up) or a
    non-blocked, non-read-only ``run_command`` (a read-only command like ``git status`` or
    the test suite does not change the workspace, so it never triggers a checkpoint). This
    is the workspace-checkpoint hook: a plan phase (which only reads) or a
    conversational/read-only act never triggers it, so a checkpoint is taken only when
    there is a real change to snapshot.
    """
    fired = False

    def snapshot_once() -> None:
        nonlocal fired
        if before_mutation is not None and not fired:
            fired = True
            before_mutation()

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
            if not is_read_only_command(command):
                snapshot_once()  # a mutating command will run — snapshot pre-change state first
            if decision == "allow" or (decision == "confirm" and not ask_on_confirm):
                _log.info("command auto-run cmd=%s", logged_command)
                return gate.execute(call, pre_authorized=True)
            _log.info("command ask cmd=%s", logged_command)  # confirm + ask → gate asks CLI
            return gate.execute(call)
        risk = gate.risk_of(call.name)
        if risk is not None and risk >= Risk.WRITE:
            snapshot_once()  # a file-mutating edit — snapshot the pre-change state first
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
    "commands from your plan, then briefly report what you did. As you work, call "
    "`update_plan` to keep your checklist current (mark each step in_progress, then done) — "
    "that's how progress is shown and how the turn knows it's finished."
)
_ROUTE_PROMPT = (
    "You route a coding request to one of two execution modes. Reply with EXACTLY one "
    "word — PLAN or CONFIRM — and nothing else.\n"
    "- PLAN: the request is multi-step, spans multiple files, is risky or destructive, or "
    "benefits from an upfront plan the user approves before any change is made.\n"
    "- CONFIRM: the request is a simple, low-risk, single-step change; act on it directly "
    "and confirm only before shell commands.\n\nRequest: "
)
_CANCELLED_REPLY = "Okay, I won't make any changes."
_ERROR_REPLY = "Something went wrong on my end, so I stopped. Nothing was changed."

_SUPERSEDE_WAIT_S = 5.0  # how long a fresh turn waits for an interrupted one to stop first
MAX_ACT_CONTINUES = 2  # auto-nudge the model past a narrate-then-stop, at most this many times
_CONTINUE_NUDGE = "Continue with the remaining steps of the task."
_REMAINING_NUDGE = "Continue with the remaining steps:\n"

# Forward-looking, first-person cues at the tail of a reply that mean the model announced a
# next step and then stopped (the narrate-then-stop failure mode) rather than finishing.
_CONTINUATION_CUES = re.compile(
    r"\b(?:"
    r"let'?s\b|let me\b(?! know)|"
    r"now i(?:'?ll| will)\b|next,? i(?:'?ll| will)\b|then i(?:'?ll| will)\b|"
    r"i(?:'?ll| will) (?:now|then|run|start|create|add|update|check|install|proceed)\b"
    r")",
    re.IGNORECASE,
)

_TODO_LINE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.*\S)\s*$")


def _is_continuation_intent(reply: str) -> bool:
    """Whether ``reply`` is a brief "I'll do X next" narration the model stopped on.

    Conservative on two axes so it never re-runs after real work: it must be *short* (a
    genuine mid-task stall is a one-liner, not a substantial summary that merely ends with
    forward-looking words) AND contain a first-person forward cue. A completion report or a
    long summary never triggers an auto-continue.
    """
    text = reply.strip()
    if not text or len(text) > 200:
        return False
    return bool(_CONTINUATION_CUES.search(text))


def _extract_todo(reply: str) -> list[str]:
    """Best-effort: pull numbered/bulleted step text from a plan reply (for future UIs)."""
    return [m.group(1) for line in reply.splitlines() if (m := _TODO_LINE.match(line))]


class CoderTurnDriver:
    """Runs one coder turn (plan → approve → act) on a worker thread over a TurnChannel.

    ``start_stream`` spawns the worker and yields its events (tool start/end, tokens) as
    they're emitted via ``TurnChannel.emit``, ending with the phase event (plan, pending,
    done, or error); ``reply_stream`` delivers the CLI's answer and streams the next
    phase the same way. Only one turn runs at a time (guarded by a lock): a second
    ``start_stream`` while a turn is actively running is rejected, while a turn is
    *parked* awaiting an answer (a CLI that died) is reclaimed.
    """

    def __init__(
        self,
        llm: Any,
        gate: PermissionGate,
        confirmer: SuspendingConfirmer,
        settings_provider: Callable[[], Settings],
        *,
        undo: Callable[[], tuple[bool, str]] | None = None,
        checkpoints: Callable[[], list[dict[str, str]]] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        """Wire the driver. ``llm`` must expose ``run_turn(text, execute, on_event=...)``.

        ``undo``/``checkpoints`` are optional closures (bound to the workspace cwd in
        ``app.py``) backing the ``/undo`` command; ``None`` disables it. ``checkpoint`` is
        the snapshot hook, called with the user's request as its label just before the act
        phase's first real change; ``None`` disables checkpointing.
        """
        self._llm = llm
        self._gate = gate
        self._confirmer = confirmer
        self._settings = settings_provider
        self._lock = threading.Lock()
        self._channel: TurnChannel | None = None
        self._awaiting = False  # True when the worker is parked awaiting an answer
        self._cancel = threading.Event()  # set by interrupt(); polled between the turn's rounds
        self._idle = threading.Event()  # set whenever no turn is active (for supersede waits)
        self._idle.set()
        self._undo = undo
        self._checkpoints = checkpoints
        self._checkpoint = checkpoint

    @staticmethod
    def _is_phase_ender(evt: dict[str, Any]) -> bool:
        """A phase-ending event closes the stream (terminal or suspend)."""
        return evt.get("status") in ("done", "error", "plan", "pending")

    def _spawn(self, text: str) -> TurnChannel:
        """Take the lock, reclaim/supersede any prior turn, start a fresh worker; return channel.

        A *parked* turn (awaiting an answer) is reclaimed immediately. An *actively running*
        turn is superseded: request cancel and wait briefly for it to stop (so a resubmit
        right after ``esc`` just works), then start fresh. Raises :class:`_TurnBusyError` only
        if the running turn won't stop in time (stuck in a long op) — the caller surfaces that.
        """
        with self._lock:
            running = self._channel is not None and not self._awaiting
        if running:
            self.interrupt()  # request the in-flight turn stop
            if not self._idle.wait(_SUPERSEDE_WAIT_S):
                raise _TurnBusyError()  # didn't stop in time (mid long LLM call / command)
        with self._lock:
            if self._channel is not None and self._awaiting:
                # Reclaim a stale parked turn: close() unblocks its parked ask() with a
                # self-decline so that worker's _run unwinds via its own channel.done(...).
                self._channel.close()
            if self._channel is not None and not self._awaiting:
                raise _TurnBusyError()  # a fresh turn slipped in during the wait
            self._cancel.clear()  # this turn starts uncancelled
            channel = TurnChannel()
            self._channel = channel
            self._awaiting = False
            self._idle.clear()
            worker = threading.Thread(
                target=self._run, args=(channel, text), name="coder-turn", daemon=True
            )
            worker.start()
        return channel

    def interrupt(self) -> bool:
        """Request the running turn stop; return whether a turn was active.

        Sets the cancel flag (polled between the turn's rounds) and closes the current channel
        so a turn parked awaiting a confirm/plan answer self-declines and unwinds. Cooperative:
        the worker stops at its next round boundary — an in-flight LLM call or tool finishes
        first (the daemon can't force-kill a worker thread). Idempotent and safe when idle.
        """
        with self._lock:
            channel = self._channel
            active = channel is not None
            if active:
                self._cancel.set()
        if channel is not None:
            channel.close()
        if active:
            _log.info("interrupt requested")
        return active

    def _resume(self, value: str, text: str) -> TurnChannel | None:
        """Deliver the answer to the parked turn; return its channel, or None if none awaits."""
        with self._lock:
            channel = self._channel
            if channel is None or not self._awaiting:
                return None
            self._awaiting = False
        channel.answer(value, text)
        return channel

    def _reclaim_or_refuse(self) -> bool:
        """Return whether it's safe to mutate; the CALLER MUST HOLD ``self._lock``.

        ``True`` when idle, or when a *parked* turn (CLI awaiting an answer that never
        came) was reclaimed via ``close()`` + reset — mirrors ``_spawn``. ``False`` when a
        turn is *actively running*: a mutation must not interleave with a live turn's
        gate/confirmer/session. Callers keep the lock held through the mutation itself so a
        concurrent ``_spawn``/mutator can't slip in between the check and the change.
        """
        if self._channel is not None and not self._awaiting:
            return False
        if self._channel is not None and self._awaiting:
            self._channel.close()
            self._channel = None
            self._awaiting = False
        return True

    def undo(self) -> tuple[bool, str]:
        """Restore the most recent checkpoint (idle only). Never raises."""
        with self._lock:
            if not self._reclaim_or_refuse():
                return False, "A coding turn is running — finish or cancel it first."
            if self._undo is None:
                return False, "Undo isn't available here."
            _log.info("undo requested")
            try:
                ok, msg = self._undo()
            except Exception:  # a mutator must always return, never raise into the daemon
                _log.exception("undo failed")
                return False, "Undo failed unexpectedly."
            _log.info("undo done ok=%s", ok)
            return ok, msg

    def list_checkpoints(self) -> list[dict[str, str]]:
        """List checkpoints (read-only; no lock)."""
        return self._checkpoints() if self._checkpoints is not None else []

    def resume(self, session_id: str) -> bool:
        """Resume a stored session (idle only). False if a turn is running."""
        with self._lock:
            if not self._reclaim_or_refuse():
                return False
            _log.info("resume session id=%s", session_id)
            try:
                return bool(self._llm.resume(session_id))
            except Exception:  # a mutator must always return, never raise into the daemon
                _log.exception("resume failed")
                return False

    def new_session(self) -> bool:
        """Start a fresh session (idle only). False if a turn is running."""
        with self._lock:
            if not self._reclaim_or_refuse():
                return False
            _log.info("new session")
            try:
                self._llm.new_session()
            except Exception:  # a mutator must always return, never raise into the daemon
                _log.exception("new_session failed")
                return False
            return True

    def _drain(self, channel: TurnChannel) -> Iterator[dict[str, Any]]:
        """Yield events from the channel until (and including) a phase-ender.

        State mutations are guarded by ``self._channel is channel`` so a superseded turn's
        drain can never clobber the turn that replaced it (turns can now be interrupted and
        superseded). Terminal cleanup is also done by the worker's ``finally`` — so the daemon
        frees even if the client disconnected and this drain stopped being consumed.
        """
        while True:
            event = channel.poll()
            if self._is_phase_ender(event):
                with self._lock:
                    if self._channel is channel:
                        if event.get("status") in ("done", "error"):
                            self._channel = None
                            self._awaiting = False
                            self._idle.set()
                        else:
                            self._awaiting = True
                yield event
                return
            yield event

    def start_stream(self, text: str) -> Iterator[dict[str, Any]]:
        """Begin a turn; yield its events (tool/phase) until the phase ends."""
        try:
            channel = self._spawn(text)
        except _TurnBusyError:
            yield {"status": "error", "reply": "A coding turn is already running."}
            return
        yield from self._drain(channel)

    def reply_stream(self, value: str, text: str = "") -> Iterator[dict[str, Any]]:
        """Deliver the CLI's answer; yield the next phase's events until it ends."""
        channel = self._resume(value, text)
        if channel is None:
            yield {"status": "error", "reply": "No coding turn is awaiting a reply."}
            return
        yield from self._drain(channel)

    def _run(self, channel: TurnChannel, text: str) -> None:
        """Worker body: drive plan→approve→act per the autonomy dial. Never raises.

        Sets the confirmer's channel first thing, on THIS (worker) thread — the
        confirmer keys its active channel by thread-local, so this must run on the
        same thread that will later call ``gate.execute`` (and thus ``confirm``)
        during the act phase.
        """
        self._confirmer.set_channel(channel)
        try:
            mode = self._settings().coding_autonomy
            if mode == "auto":  # auto-select: route this request to plan or confirm
                mode = self._route(text)
            if mode == "plan":
                outcome, payload = self._plan_loop(channel, text)
                if outcome == "cancel":
                    channel.done(_CANCELLED_REPLY)
                    return
                if outcome == "reply":  # conversational / no actionable plan — just answer
                    channel.done(payload if isinstance(payload, str) else "")
                    return
                # outcome == "act": the plan was approved, so its commands run pre-authorized.
                # ``payload`` carries the approved todo steps, seeding the act phase checklist.
                todos = payload if isinstance(payload, list) else []
                reply = self._act(
                    channel, ask_on_confirm=False, request_text=text, approved_todos=todos
                )
            else:  # confirm: no plan gate; ask before each non-allowlisted command
                reply = self._act(channel, first_text=text, ask_on_confirm=True, request_text=text)
            channel.done(reply)
        except Exception:  # a turn must always terminate with a reply for the CLI
            _log.exception("coder turn failed")
            channel.done(_ERROR_REPLY)
        finally:
            # The worker owns end-of-turn cleanup: clear the active-turn state so the daemon
            # frees even when the client disconnected (esc) and _drain stopped being consumed.
            # Guarded so a superseding turn's channel is never clobbered.
            with self._lock:
                if self._channel is channel:
                    self._channel = None
                    self._awaiting = False
                    self._idle.set()

    def _plan_loop(self, channel: TurnChannel, text: str) -> tuple[str, str | list[str]]:
        """Plan (read-only), then decide the next step.

        Returns one of:
            ``("reply", text)`` — the turn was conversational (a greeting, a question the
                coder answered from read-only tools, or a request for clarification): there
                is nothing to approve, so answer directly without a plan-approval gate.
            ``("act", todo)`` — the user approved an actionable plan; run the act phase with
                ``todo`` (the parsed numbered/bulleted steps) seeding its checklist.
            ``("cancel", "")`` — the user rejected the plan.

        An actionable plan is detected by the presence of numbered/bulleted todo steps
        (:func:`_extract_todo`); a reply with none is treated as conversational.
        """
        request = text
        while True:
            if self._cancel.is_set():
                return "cancel", ""
            executor = read_only_executor(self._gate)
            prompt = _PLAN_PROMPT_PREFIX + request
            reply = self._llm.run_turn(
                prompt, executor, on_event=channel.emit, should_cancel=self._cancel.is_set
            )
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
                return "act", todo
            if value in {"reject", "no", "n"}:
                _log.info("plan rejected")
                return "cancel", ""
            request = answer.get("text") or text  # refine: re-plan with the feedback
            _log.info("plan refined")

    def _act(
        self,
        channel: TurnChannel,
        *,
        first_text: str | None = None,
        ask_on_confirm: bool,
        request_text: str,
        approved_todos: list[str] | None = None,
    ) -> str:
        """Run the act phase; ``ask_on_confirm`` gates each non-allowlisted command.

        The *resolved* mode sets ``ask_on_confirm``: an approved ``plan`` runs its commands
        pre-authorized (``False``); ``confirm`` asks before each non-allowlisted command
        (``True``). Both still refuse blocklisted commands and stay within the cwd jail. A
        workspace checkpoint labelled ``request_text`` (the user's original request) is taken
        just before the first real change — never on a read-only act.

        ``approved_todos`` seeds the living checklist (:class:`PlanState`): the model marks
        each step via the ``update_plan`` tool, which reaches this turn through the
        ``plan_sink`` ContextVar set below. The turn completes when every step is settled
        (done/blocked); while steps remain it is nudged to continue with the open ones. For a
        model that never calls the tool (or the confirm/no-plan path, where ``approved_todos``
        is ``None``), it falls back to the ``_is_continuation_intent`` narration heuristic.
        """
        settings = self._settings()
        before_mutation: Callable[[], None] | None = None
        cp = self._checkpoint
        if cp is not None:

            def _snapshot() -> None:  # snapshot the workspace before the first change
                try:
                    cp(request_text)
                except Exception:  # a checkpoint failure must never break the turn
                    _log.exception("checkpoint failed; continuing turn")

            before_mutation = _snapshot

        executor = act_executor(
            self._gate,
            settings.command_allowlist,
            settings.command_blocklist,
            ask_on_confirm=ask_on_confirm,
            before_mutation=before_mutation,
        )
        prompt = first_text if first_text is not None else _ACT_PROMPT

        # The living checklist the act phase is driven by: seeded from the approved plan,
        # then replaced by the model's update_plan payloads. Completion is intent-defined
        # (all steps settled), with the narration heuristic kept only as a fallback.
        state = PlanState(approved_todos or [])

        def _publish(todos: list[dict[str, str]]) -> None:
            """The plan_sink: apply the model's update_plan payload + stream a delta event."""
            state.replace(todos)
            channel.emit(
                {
                    "type": "plan_update",
                    "todos": [{"step": item.step, "status": item.status} for item in state.items],
                }
            )

        token = plan_sink.set(_publish)
        try:
            reply: str = self._llm.run_turn(
                prompt, executor, on_event=channel.emit, should_cancel=self._cancel.is_set
            )
            # Continue while the task isn't finished: a cooperating model completes exactly
            # when its todos are all settled; one that ignores update_plan falls back to the
            # narrate-then-stop heuristic. Either way MAX_ACT_CONTINUES caps the nudges (each
            # nudge is a fresh run_turn, itself bounded by the harness round backstop).
            continues = 0
            while continues < MAX_ACT_CONTINUES:
                if self._cancel.is_set():
                    break  # interrupted — stop nudging and return what we have
                if state.used():
                    if state.all_settled():
                        break  # every step done/blocked → the task is complete
                    nudge = _REMAINING_NUDGE + state.remaining_text()
                elif _is_continuation_intent(reply):
                    # The model ignored update_plan — fall back to the narration heuristic.
                    nudge = _CONTINUE_NUDGE
                else:
                    break
                continues += 1
                _log.info(
                    "act continue %d/%d settled=%s summary=%s",
                    continues,
                    MAX_ACT_CONTINUES,
                    state.all_settled(),
                    state.summary(),
                )
                reply = self._llm.run_turn(
                    nudge, executor, on_event=channel.emit, should_cancel=self._cancel.is_set
                )
        finally:
            plan_sink.reset(token)
        _log.info("turn done")
        return reply

    def _route(self, text: str) -> str:
        """Auto-select the execution mode for ``text``: ``"plan"`` or ``"confirm"``.

        A lightweight one-shot classification (no tools, no history): multi-step, risky, or
        multi-file work → ``"plan"`` (propose a plan the user approves first); a simple,
        low-risk change → ``"confirm"`` (act directly, confirming shell commands). Any
        failure or unparseable reply defaults to ``"plan"`` — the safest, since it puts an
        approval checkpoint before any change.
        """
        try:
            raw = self._llm.complete(_ROUTE_PROMPT + text)
        except Exception:  # never let routing break a turn — fall back to the safe default
            _log.exception("auto route failed; defaulting to plan")
            return "plan"
        decision = "confirm" if raw.strip().upper().startswith("CONFIRM") else "plan"
        _log.info("auto routed mode=%s", decision)
        return decision
