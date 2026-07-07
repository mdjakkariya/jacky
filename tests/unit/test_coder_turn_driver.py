"""CoderTurnDriver: plan/approve/reject/refine flow, act suspension, dial modes."""

from __future__ import annotations

import queue
import threading

from autobot.agent.coder_turn import CoderTurnDriver, SuspendingConfirmer, TurnChannel
from autobot.config import Settings
from autobot.core.types import Risk, ToolCall
from autobot.tools.audit import AuditLog
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry, ToolSpec

_JOIN_TIMEOUT_S = 5.0


class _ScriptedLLM:
    """A fake ReloadableLanguageModel.

    Plan turns return a plan; act turns drive the scripted tool calls through the
    executor, then return the act reply.
    """

    def __init__(self, plan_reply: str, act_reply: str, act_calls: list[ToolCall] | None = None):
        self.plan_reply = plan_reply
        self.act_reply = act_reply
        self.act_calls = act_calls or []
        self.plans = 0

    def run_turn(self, user_text: str, execute, on_event=None) -> str:  # type: ignore[no-untyped-def]
        if "PLANNING" in user_text:
            self.plans += 1
            return self.plan_reply
        for call in self.act_calls:
            execute(call)
        return self.act_reply


def _coder_gate(confirmer: SuspendingConfirmer) -> PermissionGate:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="run_command",
            description="",
            parameters={},
            handler=lambda **_k: "command output",
            risk=Risk.DESTRUCTIVE,
        )
    )
    return PermissionGate(reg, AuditLog(":memory:"), confirmer)


def _driver(llm: _ScriptedLLM, autonomy: str = "plan") -> CoderTurnDriver:
    sc = SuspendingConfirmer()
    gate = _coder_gate(sc)
    settings = Settings(profile="coder", coding_autonomy=autonomy)
    return CoderTurnDriver(llm, gate, sc, settings_provider=lambda: settings)


def test_plan_then_approve_then_done() -> None:
    d = _driver(_ScriptedLLM("1. edit foo\n2. add test", "Edited foo and added a test."))
    first = list(d.start_stream("add a test for foo"))
    assert first[-1]["status"] == "plan"
    assert first[-1]["todo"] == ["edit foo", "add test"]
    final = list(d.reply_stream("approve"))
    assert final[-1] == {"status": "done", "reply": "Edited foo and added a test."}


def test_conversational_reply_skips_approval_gate() -> None:
    # A greeting/question: the plan turn has no numbered steps, so there's nothing to
    # approve — the driver must answer directly, not emit a plan-approval event.
    d = _driver(_ScriptedLLM("Hi! What would you like me to work on?", "should not act"))
    result = list(d.start_stream("hey"))
    assert result[-1] == {"status": "done", "reply": "Hi! What would you like me to work on?"}


def test_plan_reject_makes_no_changes() -> None:
    d = _driver(_ScriptedLLM("1. edit foo", "should not run"))
    list(d.start_stream("do it"))
    final = list(d.reply_stream("reject"))
    assert final[-1]["status"] == "done"
    assert "won't" in final[-1]["reply"].lower() or "not" in final[-1]["reply"].lower()


def test_plan_refine_replans_then_approve() -> None:
    llm = _ScriptedLLM("1. first plan", "done acting")
    d = _driver(llm)
    list(d.start_stream("do it"))
    again = list(d.reply_stream("refine", "also update the docs"))
    assert again[-1]["status"] == "plan" and llm.plans == 2
    final = list(d.reply_stream("approve"))
    assert final[-1]["status"] == "done"


def test_confirm_mode_asks_before_each_command() -> None:
    # confirm mode: no plan phase; act directly and suspend-to-ask before a non-allowlisted
    # command. This is the mode where the escalate-to-ask mechanism is the primary UX.
    llm = _ScriptedLLM(
        "(no plan in confirm mode)",
        "Tests passed.",
        act_calls=[
            ToolCall(name="run_command", arguments={"command": "pytest -q"}),
        ],
    )
    d = _driver(llm, autonomy="confirm")
    pending = list(d.start_stream("run the tests"))  # straight to act; pytest not allowlisted
    assert pending[-1]["status"] == "pending" and "pytest" in pending[-1]["prompt"]
    final = list(d.reply_stream("yes"))
    assert final[-1] == {"status": "done", "reply": "Tests passed."}


def test_plan_mode_does_not_reconfirm_planned_command() -> None:
    # Approving the plan IS the approval for its commands — the act phase must NOT ask a
    # second time for a planned (non-allowlisted) command; it runs pre-authorized.
    llm = _ScriptedLLM(
        "1. run the script",
        "Ran it.",
        act_calls=[
            ToolCall(name="run_command", arguments={"command": "bash check_ip.sh"}),
        ],
    )
    d = _driver(llm)  # plan mode
    assert list(d.start_stream("run the script"))[-1]["status"] == "plan"
    final = list(d.reply_stream("approve"))  # no second pending — runs straight through to done
    assert final[-1] == {"status": "done", "reply": "Ran it."}


def test_auto_mode_skips_plan_and_runs_command() -> None:
    llm = _ScriptedLLM(
        "(unused)",
        "Built.",
        act_calls=[
            ToolCall(name="run_command", arguments={"command": "npm run build"}),
        ],
    )
    d = _driver(llm, autonomy="auto")
    final = list(d.start_stream("build it"))  # no plan phase, no ask (auto)
    assert final[-1] == {"status": "done", "reply": "Built."}


def test_reply_without_active_turn_errors() -> None:
    d = _driver(_ScriptedLLM("1. x", "y"))
    assert list(d.reply_stream("yes"))[-1]["status"] == "error"


def test_reclaimed_parked_turn_does_not_block_fresh_start() -> None:
    """A fresh start_stream() while a prior turn is parked reclaims it (CLI died) cleanly.

    The stale worker's ask() must self-decline via the closed channel instead of
    hanging, so it terminates via its own channel.done(...) and the daemon never
    hangs waiting on it. The fresh turn must proceed on its own channel and return
    a normal event, not anything from the reclaimed turn.
    """
    llm = _ScriptedLLM("1. edit foo\n2. add test", "Edited foo and added a test.")
    d = _driver(llm)

    first = list(d.start_stream("add a test for foo"))  # parks awaiting plan approval
    assert first[-1]["status"] == "plan"

    # A fresh start_stream() while the first turn is parked reclaims it instead of hanging.
    second = list(d.start_stream("do something else"))
    assert second[-1]["status"] == "plan"
    assert second[-1]["reply"] == first[-1]["reply"]  # same scripted plan reply, fresh turn

    # The fresh turn proceeds normally to completion.
    final = list(d.reply_stream("approve"))
    assert final[-1] == {"status": "done", "reply": "Edited foo and added a test."}

    # The reclaimed (stale) worker thread must have terminated on its own — never left
    # parked — by self-declining via the closed channel's reject. Give it a bounded
    # window to finish; a hang here would mean the reclaim leaked a parked thread.
    for thread in threading.enumerate():
        if thread.name == "coder-turn" and thread is not threading.current_thread():
            thread.join(timeout=_JOIN_TIMEOUT_S)
            assert not thread.is_alive(), "reclaimed coder-turn worker never terminated"


def test_confirmer_routes_each_thread_to_its_own_channel() -> None:
    """Thread-local isolation: two threads' confirm() calls never cross-wire channels.

    Simulates two "workers" on real threads, each setting its own channel and then
    confirming — the confirmer must route each thread's confirm to the channel that
    THAT thread set, never the other thread's channel.
    """
    confirmer = SuspendingConfirmer()
    channel_a = TurnChannel()
    channel_b = TurnChannel()
    channel_a.answer("yes")
    channel_b.answer("no")

    results: dict[str, bool] = {}
    ready = threading.Barrier(2, timeout=_JOIN_TIMEOUT_S)

    def worker(name: str, channel: TurnChannel) -> None:
        confirmer.set_channel(channel)
        ready.wait()  # line both threads up so set_channel calls interleave before confirm
        results[name] = confirmer.confirm("proceed?", "danger")

    t_a = threading.Thread(target=worker, args=("a", channel_a), name="worker-a")
    t_b = threading.Thread(target=worker, args=("b", channel_b), name="worker-b")
    t_a.start()
    t_b.start()
    t_a.join(timeout=_JOIN_TIMEOUT_S)
    t_b.join(timeout=_JOIN_TIMEOUT_S)

    assert not t_a.is_alive() and not t_b.is_alive()
    assert results == {"a": True, "b": False}  # each thread saw only its own channel's answer


def test_reclaim_close_does_not_leak_a_parked_confirm() -> None:
    """A turn parked mid-act (awaiting a command confirm) is reclaimed without hanging.

    Exercises close() unblocking ask() from inside SuspendingConfirmer.confirm (not
    just the plan phase), using a bounded queue.get so a regression here fails fast
    instead of hanging the test suite.
    """
    # confirm mode parks mid-act awaiting a command confirm (plan mode doesn't re-ask).
    llm = _ScriptedLLM(
        "(confirm mode: no plan)",
        "Tests passed.",
        act_calls=[ToolCall(name="run_command", arguments={"command": "pytest -q"})],
    )
    d = _driver(llm, autonomy="confirm")
    pending = list(d.start_stream("run the tests"))
    assert pending[-1]["status"] == "pending"  # parked awaiting the run_command confirm

    events: queue.Queue[dict[str, object]] = queue.Queue()

    def fresh_start() -> None:
        events.put(list(d.start_stream("a different request"))[-1])

    t = threading.Thread(target=fresh_start, name="reclaimer")
    t.start()
    t.join(timeout=_JOIN_TIMEOUT_S)
    assert not t.is_alive(), "reclaiming start_stream() hung"

    event = events.get(timeout=_JOIN_TIMEOUT_S)
    assert event["status"] == "pending"  # the fresh turn's own act confirm, not stale state
