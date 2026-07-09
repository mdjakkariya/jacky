"""Runner orchestration with a fake PTY session + fake judge (no process/LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyte")

from autobot.e2e import runner
from autobot.e2e.scenario import FileExists, Scenario


class _FakeSession:
    """Screens advance through a scripted list as the runner drives it."""

    def __init__(self, screens: list[str]) -> None:
        self._screens = screens
        self._i = 0
        self.sent: list[str] = []

    def wait_for(self, marker, timeout, poll=0.05):  # type: ignore[no-untyped-def]
        while self._i < len(self._screens) - 1 and not marker(self._screens[self._i]):
            self._i += 1
        return marker(self._screens[self._i])

    def send(self, text: str) -> None:
        self.sent.append(text)

    def send_key(self, name: str) -> None:
        self.sent.append(f"<{name}>")

    def screen_text(self) -> str:
        return self._screens[self._i]

    def raw_bytes(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


def test_unattended_auto_approves_gate_then_completes(tmp_path: Path) -> None:
    sc = Scenario(
        name="t",
        autonomy="auto",
        strategy="unattended",
        task="do X",
        success_criteria="did X",
        checks=(FileExists("hello.py"),),
    )
    screens = ["❯ ", "⏺ working", "Proceed?   [1] Yes   [2] Edit", "⏺ done\n❯ "]
    sess = _FakeSession(screens)

    def factory(argv, cwd):  # type: ignore[no-untyped-def]
        (Path(cwd) / "hello.py").write_text("hi")  # simulate the agent creating the file
        return sess

    res = runner.run_scenario(
        sc,
        port=8999,
        judge_mode="manual",
        keep=False,
        session_factory=factory,
        judge_fn=lambda *a, **k: {"pass": True},
    )
    assert res.passed is True and Path(res.report_path).exists()
    # The gate must be approved exactly once — not re-approved on every poll until the
    # 50-iteration cap silently falls through (the bug this test guards against).
    assert sess.sent.count("<1>") == 1
    assert sess.sent.count("<enter>") == 1


def test_failed_check_fails_the_result(tmp_path: Path) -> None:
    sc = Scenario(
        name="t",
        autonomy="auto",
        strategy="unattended",
        task="do X",
        success_criteria="did X",
        checks=(FileExists("missing.py"),),
    )
    sess = _FakeSession(["❯ ", "⏺ done\n❯ "])
    res = runner.run_scenario(
        sc,
        port=8999,
        judge_mode="manual",
        session_factory=lambda argv, cwd: sess,
        judge_fn=lambda *a, **k: None,
    )
    assert res.passed is False  # deterministic check failed
