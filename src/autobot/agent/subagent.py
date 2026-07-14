"""Subagents: a coordinator spawns focused research agents that run off the turn.

A subagent is the ``kind="agent"`` instance of the async-task primitive (the ``command``
instance is the backgrounded shell command). It reuses the *whole* delivery path already
built: a fresh, isolated :class:`~autobot.agent.harness.AgentHarness` (its own model +
:class:`~autobot.agent.session.Session`, so cost is attributed per agent) runs one turn on
a worker thread; on completion its findings are pushed to the *parent* session's
notification inbox, so auto-resume re-engages the coordinator with the result — no polling.

Safety composes rather than weakens: a subagent runs through :func:`subagent_executor`,
which is read-only (it refuses anything at or above :data:`~autobot.core.types.Risk.WRITE`)
and refuses to spawn further subagents — so a subagent is never more privileged than the
coordinator and recursion can't run away. Concurrency is capped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from autobot.core.streaming import active_session_id
from autobot.core.types import Risk, ToolCall, ToolResult
from autobot.logging_setup import get_logger
from autobot.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from autobot.agent.harness import AgentHarness
    from autobot.core.types import ToolExecutor
    from autobot.tasks import NotificationInbox, TaskRegistry
    from autobot.tools.permission import PermissionGate

_log = get_logger("coder")

_MAX_CONCURRENT_SUBAGENTS = 4  # runaway guard: at most this many agent tasks in flight
_RESULT_CAP = 4000  # chars of a subagent's reply stored on the task row (full text still delivered)
SUBAGENT_MAX_ROUNDS = 20  # per-subagent turn budget — tighter than the coordinator's ceiling

# Tools a subagent must never call — coordinator-only. Blocks recursion (a subagent spawning
# subagents) even though the tool is in the shared registry.
_COORDINATOR_ONLY = frozenset({"spawn_agent"})

_SUBAGENT_FRAMING = (
    "You are a focused research subagent working for a coding agent. Investigate the task "
    "below using READ-ONLY tools only (read files, search, list, map the repo). You CANNOT "
    "modify files, run commands, or spawn further subagents — those are refused. When done, "
    "reply with a concise, self-contained summary of your findings: the direct answer plus "
    "the key facts and `file:line` references, so the coordinator can act on it without "
    "re-doing your work.\n\nTask: "
)


def subagent_executor(gate: PermissionGate) -> ToolExecutor:
    """An executor for a subagent turn: read-only, and never spawns more subagents.

    Refuses coordinator-only tools (:data:`_COORDINATOR_ONLY`) and anything at or above
    :data:`~autobot.core.types.Risk.WRITE` (edits, ``run_command``); read-only tools run
    through the same gate the coordinator uses. So a subagent can explore but never mutate
    the workspace or fan out again.
    """

    def execute(call: ToolCall) -> ToolResult:
        if call.name in _COORDINATOR_ONLY:
            return ToolResult(name=call.name, content="A subagent can't spawn subagents.", ok=False)
        risk = gate.risk_of(call.name)
        if risk is not None and risk >= Risk.WRITE:
            return ToolResult(
                name=call.name,
                content="A subagent is read-only and can't modify the workspace or run commands.",
                ok=False,
            )
        return gate.execute(call)

    return execute


class SubagentRunner:
    """Spawns and tracks subagents on the shared async-task registry."""

    def __init__(
        self,
        make_harness: Callable[[], AgentHarness],
        gate: PermissionGate,
        registry: TaskRegistry,
        inbox: NotificationInbox,
        *,
        max_concurrent: int = _MAX_CONCURRENT_SUBAGENTS,
    ) -> None:
        """Wire the runner.

        Args:
            make_harness: Factory returning a fresh, isolated harness (own model + session)
                for one subagent — never shared, since the model adapters keep per-turn state.
            gate: The permission gate; the subagent runs read-only through it.
            registry: The async-task registry (subagents are ``kind="agent"`` rows).
            inbox: The notification inbox; a subagent's result is pushed to its parent session.
            max_concurrent: Cap on agent tasks in flight at once (runaway guard).
        """
        self._make_harness = make_harness
        self._gate = gate
        self._registry = registry
        self._inbox = inbox
        self._max = max(1, max_concurrent)

    def spawn(self, task: str, label: str = "") -> str:
        """Register + start a subagent for ``task``; return an immediate ack (never blocks)."""
        task = (task or "").strip()
        if not task:
            return "What should the subagent do? Give it a concrete research task."
        running = self._registry.running_count(kind="agent")
        if running >= self._max:
            return (
                f"Already running {running}/{self._max} subagents — wait for one to finish "
                "(its result will arrive on its own) before spawning another."
            )
        parent = active_session_id.get()
        label = label.strip() or (task[:60] + ("…" if len(task) > 60 else ""))
        row = self._registry.add(kind="agent", session_id=parent, label=label)

        def _worker() -> None:
            try:
                harness = self._make_harness()
                reply = harness.run_turn(_SUBAGENT_FRAMING + task, subagent_executor(self._gate))
            except Exception as exc:  # a subagent must never crash the daemon
                self._registry.mark_failed(row.id, result=f"error: {exc}", returncode=None)
                self._inbox.push(parent, f"Subagent {row.id} ({label}) failed: {exc}")
                _log.exception("subagent %s failed", row.id)
                return
            self._registry.mark_done(row.id, result=reply[:_RESULT_CAP], returncode=0)
            self._inbox.push(parent, f"Subagent {row.id} ({label}) finished:\n{reply}")
            _log.info("subagent %s finished chars=%d", row.id, len(reply))

        threading.Thread(target=_worker, name=f"subagent-{row.id}", daemon=True).start()
        _log.info("spawned subagent %s label=%s", row.id, label)
        return (
            f"Started subagent {row.id}: {label}. It's researching in the background; its "
            "findings will arrive on their own when it finishes. Launch other INDEPENDENT "
            "subagents now if that helps, then continue — do NOT wait or poll for it."
        )


def register_subagent_tool(registry: ToolRegistry, runner: SubagentRunner) -> None:
    """Register the ``spawn_agent`` tool (coordinator-only; ``Risk.READ_ONLY``)."""
    registry.register(
        ToolSpec(
            name="spawn_agent",
            description=(
                "Delegate a focused, READ-ONLY research task to a subagent that works in "
                "parallel (e.g. 'find every call site of X', 'summarize how auth flows through "
                "these modules', 'locate the config that controls Y'). Use it to fan out "
                "INDEPENDENT investigations at once instead of exploring serially: spawn each, "
                "then continue — each subagent's findings are delivered to you automatically "
                "when it finishes, so never wait or poll. Subagents are read-only (they can't "
                "edit files, run commands, or spawn more subagents); do the actual changes "
                "yourself with their findings. Not for trivial single reads — just read those."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The self-contained research task for the subagent.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional short label for progress display.",
                    },
                },
                "required": ["task"],
            },
            handler=lambda task="", label="": runner.spawn(task, label),
            risk=Risk.READ_ONLY,
            core=True,
            ack="Spawning a research subagent.",
        )
    )
    _log.info("subagent tool registered (spawn_agent)")
