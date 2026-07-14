"""End-to-end subagent: a real AgentHarness reads a real file through the real gate.

No fakes on the path that matters — only the model's *decisions* are scripted (a live LLM
isn't available in CI). A real :class:`SubagentRunner` spawns a real
:class:`~autobot.agent.harness.AgentHarness`, which runs the real round loop, calls the real
``read_file`` tool through the real :class:`~autobot.tools.permission.PermissionGate` and
workspace jail, and returns a summary; the real registry marks the task done and the real
inbox delivers the result to the parent session. Proves the whole spawn → run → deliver path
and that :func:`subagent_executor` really blocks a write while allowing the read.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from autobot.agent.chat_model import ChatResponse
from autobot.agent.harness import AgentHarness
from autobot.agent.session import Session
from autobot.agent.session_store import SessionStore
from autobot.agent.subagent import SUBAGENT_MAX_ROUNDS, SubagentRunner
from autobot.core.streaming import active_session_id
from autobot.core.types import ToolCall, ToolResult
from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.audit import AuditLog
from autobot.tools.code.tools import register_code_tools
from autobot.tools.permission import PermissionGate
from autobot.tools.registry import ToolRegistry


class _GrantAll:
    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return True

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default


class _ScriptedResearchModel:
    """A ChatModel that reads one file, then summarizes it with what it read.

    Round 1: call ``read_file`` on ``path`` AND (to prove the read-only guard) ``edit_file``.
    Round 2: reply with a summary embedding whatever ``read_file`` actually returned. It also
    records whether the edit was refused, so the test can assert the guard fired.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._round = 0
        self._read = ""
        self.edit_refused = False

    def begin_turn(self, session: Session, user_text: str) -> None:
        session.history.append({"role": "user", "content": user_text})

    def send(self, session: Session, on_event: Any = None) -> ChatResponse:
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                text="",
                tool_calls=[
                    ToolCall(name="read_file", arguments={"path": self._path}),
                    ToolCall(
                        name="edit_file",
                        arguments={"path": self._path, "find": "x", "replace": "y"},
                    ),
                ],
            )
        return ChatResponse(
            text=f"FINDINGS: the file contains -> {self._read.strip()}", tool_calls=[]
        )

    def record_results(self, session: Session, results: list[tuple[ToolCall, ToolResult]]) -> None:
        for call, result in results:
            if call.name == "read_file":
                self._read = result.content
            if call.name == "edit_file" and not result.ok:
                self.edit_refused = True

    def handle_discovery(self, session: Session, call: ToolCall) -> str | None:
        return None

    def final_answer_no_tools(self, session: Session) -> str:
        return "no answer"

    def finalize_turn(self, session: Session) -> list[dict[str, Any]]:
        return []

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        return ""


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_subagent_reads_a_real_file_and_delivers_findings(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("THE-SECRET-VALUE-42\n")

    confirmer = _GrantAll()
    broker = AccessBroker(
        AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path), confirmer
    )
    registry = ToolRegistry()
    register_code_tools(registry, broker)  # real read_file / edit_file / etc.
    gate = PermissionGate(registry, AuditLog(":memory:"), confirmer)  # type: ignore[arg-type]

    from autobot.tasks import NotificationInbox, TaskRegistry

    task_registry = TaskRegistry()
    inbox = NotificationInbox()
    store = SessionStore(str(tmp_path / "sessions"))
    model = _ScriptedResearchModel("notes.txt")

    def make_harness() -> AgentHarness:
        return AgentHarness(
            model, store, cwd=str(tmp_path), model_name="scripted", max_rounds=SUBAGENT_MAX_ROUNDS
        )

    runner = SubagentRunner(make_harness, gate, task_registry, inbox)
    token = active_session_id.set("parent-session")
    try:
        ack = runner.spawn("read notes.txt and report exactly what it contains", "read-notes")
    finally:
        active_session_id.reset(token)
    assert "task-1" in ack

    assert _wait(lambda: inbox.pending("parent-session") > 0), "subagent result never delivered"
    note = inbox.drain("parent-session")[0]
    # The subagent really read the real file through the real gate + read_file tool.
    assert "THE-SECRET-VALUE-42" in note
    assert "task-1" in note

    row = task_registry.get("task-1")
    assert row is not None and row.kind == "agent" and row.status == "done"
    # The read-only guard really refused the edit the model attempted.
    assert model.edit_refused is True
