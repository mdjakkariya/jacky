"""CoderTurnDriver: plan/approve/reject/refine flow, act suspension, dial modes."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

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

    def __init__(
        self,
        plan_reply: str,
        act_reply: str,
        act_calls: list[ToolCall] | None = None,
        route: str = "PLAN",
        route_error: bool = False,
    ):
        self.plan_reply = plan_reply
        self.act_reply = act_reply
        self.act_calls = act_calls or []
        self.plans = 0
        self.route = route  # what the auto-mode router classifier returns
        self.route_error = route_error  # if True, complete() raises (router failure)
        self.completes = 0

    def run_turn(self, user_text: str, execute, on_event=None) -> str:  # type: ignore[no-untyped-def]
        if "PLANNING" in user_text:
            self.plans += 1
            return self.plan_reply
        for call in self.act_calls:
            execute(call)
        return self.act_reply

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """Router classification stub: return the scripted route word (or raise)."""
        self.completes += 1
        if self.route_error:
            raise RuntimeError("router unavailable")
        return self.route


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
    reg.register(
        ToolSpec(
            name="read_file",
            description="",
            parameters={},
            handler=lambda **_k: "file contents",
            risk=Risk.READ_ONLY,
        )
    )
    reg.register(
        ToolSpec(
            name="write_file",
            description="",
            parameters={},
            handler=lambda **_k: "wrote",
            risk=Risk.WRITE,
        )
    )
    return PermissionGate(reg, AuditLog(":memory:"), confirmer)


def _driver(
    llm: _ScriptedLLM,
    autonomy: str = "plan",
    checkpoint: Callable[[str], None] | None = None,
) -> CoderTurnDriver:
    sc = SuspendingConfirmer()
    gate = _coder_gate(sc)
    settings = Settings(profile="coder", coding_autonomy=autonomy)
    return CoderTurnDriver(
        llm,
        gate,
        sc,
        settings_provider=lambda: settings,
        checkpoint=checkpoint,
    )


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
            ToolCall(name="run_command", arguments={"command": "bash deploy.sh"}),
        ],
    )
    d = _driver(llm, autonomy="confirm")
    pending = list(d.start_stream("run the tests"))  # straight to act; deploy.sh isn't read-only
    assert pending[-1]["status"] == "pending" and "deploy.sh" in pending[-1]["prompt"]
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


def test_auto_routes_to_plan_mode() -> None:
    # auto mode = auto-select: the router classifies this as PLAN, so it behaves like plan
    # mode — propose a plan, gate on approval, then act.
    llm = _ScriptedLLM("1. edit foo\n2. add test", "Edited and tested.", route="PLAN")
    d = _driver(llm, autonomy="auto")
    first = list(d.start_stream("do a multi-step refactor"))
    assert first[-1]["status"] == "plan"
    assert first[-1]["todo"] == ["edit foo", "add test"]
    final = list(d.reply_stream("approve"))
    assert final[-1] == {"status": "done", "reply": "Edited and tested."}
    assert llm.completes == 1  # routed exactly once


def test_auto_routes_to_confirm_mode() -> None:
    # The router classifies this as CONFIRM, so it behaves like confirm mode — no plan
    # gate, act directly, but suspend-to-ask before a non-allowlisted command.
    llm = _ScriptedLLM(
        "(unused)",
        "Ran it.",
        act_calls=[ToolCall(name="run_command", arguments={"command": "bash deploy.sh"})],
        route="CONFIRM",
    )
    d = _driver(llm, autonomy="auto")
    pending = list(d.start_stream("run the tests"))
    assert pending[-1]["status"] == "pending" and "deploy.sh" in pending[-1]["prompt"]
    assert llm.plans == 0  # confirm route never runs a planning turn
    final = list(d.reply_stream("yes"))
    assert final[-1] == {"status": "done", "reply": "Ran it."}


def test_auto_route_failure_defaults_to_plan() -> None:
    # If the router call fails, default to PLAN (safest — the user approves before changes).
    llm = _ScriptedLLM("1. edit foo", "done", route_error=True)
    d = _driver(llm, autonomy="auto")
    first = list(d.start_stream("do something"))
    assert first[-1]["status"] == "plan"
    list(d.reply_stream("reject"))  # resolve the parked turn so its worker terminates


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
        act_calls=[ToolCall(name="run_command", arguments={"command": "bash deploy.sh"})],
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


def test_undo_delegates_when_idle() -> None:
    d = _driver(_ScriptedLLM("1. x", "done"))
    d._undo = lambda: (True, "Reverted to before edit")  # injected closure
    ok, msg = d.undo()
    assert ok is True and "Reverted" in msg


def test_undo_refused_while_turn_running() -> None:
    d = _driver(_ScriptedLLM("1. x", "done"))
    d._undo = lambda: (True, "reverted")
    # Simulate an actively-running (not parked) turn.
    d._channel = TurnChannel()
    d._awaiting = False
    ok, msg = d.undo()
    assert ok is False and "running" in msg.lower()


def test_resume_reclaims_parked_turn_then_delegates() -> None:
    llm = _ScriptedLLM("1. x", "done")
    resumed: list[str] = []

    def _fake_resume(sid: str) -> bool:
        resumed.append(sid)
        return True

    llm.resume = _fake_resume  # type: ignore[attr-defined]
    d = _driver(llm)
    d._channel = TurnChannel()
    d._awaiting = True  # parked
    assert d.resume("sess-1") is True
    assert resumed == ["sess-1"]
    assert d._channel is None and d._awaiting is False


def test_new_session_refused_while_running() -> None:
    llm = _ScriptedLLM("1. x", "done")
    llm.new_session = lambda: None  # type: ignore[attr-defined]
    d = _driver(llm)
    d._channel = TurnChannel()
    d._awaiting = False
    assert d.new_session() is False


def test_list_checkpoints_delegates() -> None:
    d = _driver(_ScriptedLLM("1. x", "done"))
    d._checkpoints = lambda: [{"ref": "refs/jack/checkpoints/0", "sha": "a", "label": "x"}]
    assert d.list_checkpoints()[0]["label"] == "x"


def test_checkpoint_taken_once_before_first_act_mutation() -> None:
    # A checkpoint is snapshotted exactly once, labelled with the user's ORIGINAL request,
    # just before the act phase's first workspace-changing tool — reads before it don't
    # trigger it, and a later edit doesn't snapshot again.
    labels: list[str] = []
    llm = _ScriptedLLM(
        "1. edit foo",
        "Edited foo.",
        act_calls=[
            ToolCall(name="read_file", arguments={"path": "foo.py"}),  # read → no snapshot
            ToolCall(name="write_file", arguments={"path": "foo.py"}),  # first change → snapshot
            ToolCall(name="write_file", arguments={"path": "bar.py"}),  # later change → no re-snap
        ],
    )
    d = _driver(llm, checkpoint=labels.append)
    assert list(d.start_stream("add a test for foo"))[-1]["status"] == "plan"
    assert list(d.reply_stream("approve"))[-1]["status"] == "done"
    assert labels == ["add a test for foo"]


def test_no_checkpoint_for_conversational_turn() -> None:
    # A greeting/question never reaches the act phase, so nothing is snapshotted.
    labels: list[str] = []
    d = _driver(_ScriptedLLM("Hi! What can I help with?", "unused"), checkpoint=labels.append)
    list(d.start_stream("hey"))
    assert labels == []


def test_no_checkpoint_when_act_only_reads() -> None:
    # An approved plan whose act phase only reads changes nothing → no checkpoint.
    llm = _ScriptedLLM(
        "1. look at foo",
        "foo does X.",
        act_calls=[ToolCall(name="read_file", arguments={"path": "foo.py"})],
    )
    labels: list[str] = []
    d = _driver(llm, checkpoint=labels.append)
    list(d.start_stream("what does foo do"))
    list(d.reply_stream("approve"))
    assert labels == []


def test_no_checkpoint_when_plan_rejected() -> None:
    labels: list[str] = []
    d = _driver(_ScriptedLLM("1. edit foo", "unused"), checkpoint=labels.append)
    list(d.start_stream("do it"))
    list(d.reply_stream("reject"))
    assert labels == []


def test_checkpoint_failure_does_not_break_the_turn() -> None:
    # A snapshot hook that raises must be swallowed — the edit still runs and the turn
    # completes normally (the "checkpoint failure never breaks the turn" contract).
    def boom(_label: str) -> None:
        raise RuntimeError("checkpoint blew up")

    llm = _ScriptedLLM(
        "1. edit foo",
        "Edited foo.",
        act_calls=[ToolCall(name="write_file", arguments={"path": "foo.py"})],
    )
    d = _driver(llm, checkpoint=boom)
    list(d.start_stream("edit foo"))
    final = list(d.reply_stream("approve"))
    assert final[-1] == {"status": "done", "reply": "Edited foo."}
