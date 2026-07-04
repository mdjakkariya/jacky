"""CoderTurnDriver: plan/approve/reject/refine flow, act suspension, dial modes."""

from __future__ import annotations

from autobot.agent.coder_turn import CoderTurnDriver, SuspendingConfirmer
from autobot.config import Settings
from autobot.core.types import Risk, ToolCall
from autobot.tools.audit import AuditLog
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry, ToolSpec


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

    def run_turn(self, user_text: str, execute) -> str:  # type: ignore[no-untyped-def]
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
    first = d.start("add a test for foo")
    assert first["status"] == "plan"
    assert first["todo"] == ["edit foo", "add test"]
    final = d.reply("approve")
    assert final == {"status": "done", "reply": "Edited foo and added a test."}


def test_plan_reject_makes_no_changes() -> None:
    d = _driver(_ScriptedLLM("1. edit foo", "should not run"))
    d.start("do it")
    final = d.reply("reject")
    assert final["status"] == "done"
    assert "won't" in final["reply"].lower() or "not" in final["reply"].lower()


def test_plan_refine_replans_then_approve() -> None:
    llm = _ScriptedLLM("1. first plan", "done acting")
    d = _driver(llm)
    d.start("do it")
    again = d.reply("refine", "also update the docs")
    assert again["status"] == "plan" and llm.plans == 2
    final = d.reply("approve")
    assert final["status"] == "done"


def test_act_suspends_to_ask_then_resumes() -> None:
    llm = _ScriptedLLM(
        "1. run tests",
        "Tests passed.",
        act_calls=[
            ToolCall(name="run_command", arguments={"command": "pytest -q"}),
        ],
    )
    d = _driver(llm)  # plan mode; pytest not allowlisted → confirm → ask
    d.start("run the tests")
    pending = d.reply("approve")
    assert pending["status"] == "pending" and "pytest" in pending["prompt"]
    final = d.reply("yes")
    assert final == {"status": "done", "reply": "Tests passed."}


def test_auto_mode_skips_plan_and_runs_command() -> None:
    llm = _ScriptedLLM(
        "(unused)",
        "Built.",
        act_calls=[
            ToolCall(name="run_command", arguments={"command": "npm run build"}),
        ],
    )
    d = _driver(llm, autonomy="auto")
    final = d.start("build it")  # no plan phase, no ask (auto)
    assert final == {"status": "done", "reply": "Built."}


def test_reply_without_active_turn_errors() -> None:
    d = _driver(_ScriptedLLM("1. x", "y"))
    assert d.reply("yes")["status"] == "error"
