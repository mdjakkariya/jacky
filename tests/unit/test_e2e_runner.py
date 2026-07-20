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

    def wait_until_stable(self, marker, timeout, *, stable_for=1.0, poll=0.05):  # type: ignore[no-untyped-def]
        # The scripted screens are already discrete resting states, so "stable" reduces to
        # "advance to the next screen that satisfies the marker" — same as wait_for.
        return self.wait_for(marker, timeout, poll)  # type: ignore[no-untyped-call]

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
    screens = [
        "❯ ",  # pre-send idle
        "⠹ Working…  ·  esc to interrupt · 1s",  # turn_started (spinner)
        "Run this command?\n\n  $ x\nApprove? [y]es · [n]o",  # awaiting_reply — a LIVE gate
        "⎿  Edited hello.py\n⠋ Working…  ·  esc to interrupt · 2s",  # act running
        "⏺ done\n❯ ",  # settled idle
    ]
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
    # The gate must be approved exactly once (type "y" + Enter) — not re-approved on every
    # poll until the 50-iteration cap silently falls through.
    assert sess.sent.count("y") == 1


def test_unattended_does_not_reapprove_a_stale_gate_card(tmp_path: Path) -> None:
    # After a gate is answered the single-key prompt erases itself, so a stale card can't
    # linger. This still guards that the driver approves once and settles at idle (it must
    # not re-approve on a frame where the idle prompt hasn't repainted yet).
    sc = Scenario(
        name="t",
        autonomy="auto",
        strategy="unattended",
        task="do X",
        success_criteria="did X",
        checks=(FileExists("hello.py"),),
    )
    screens = [
        "❯ ",
        "⠹ Working…  ·  esc to interrupt · 1s",  # turn_started
        "Run this command?\n\n  $ x\nApprove? [y]es · [n]o",  # live gate → approve once
        "⏺ done\n❯ ",  # answered; the gate affordance cleared, settled idle
    ]
    sess = _FakeSession(screens)

    def factory(argv, cwd):  # type: ignore[no-untyped-def]
        (Path(cwd) / "hello.py").write_text("hi")
        return sess

    res = runner.run_scenario(
        sc, port=8999, judge_mode="manual", session_factory=factory, judge_fn=lambda *a, **k: None
    )
    assert res.passed is True
    assert sess.sent.count("y") == 1  # approved exactly once (type "y" + Enter)


def test_bundle_captures_observability_files(tmp_path: Path) -> None:
    # The bundle must be self-contained: effective settings + the coder's session
    # transcript are captured, not left as the empty placeholders they used to be.
    sc = Scenario(
        name="obs",
        autonomy="auto",
        strategy="unattended",
        task="do X",
        success_criteria="did X",
        checks=(FileExists("hello.py"),),
    )
    sess = _FakeSession(["❯ ", "⏺ done\n❯ "])

    def factory(argv, cwd):  # type: ignore[no-untyped-def]
        p = Path(cwd)
        (p / "hello.py").write_text("hi")
        sdir = p / ".jack" / "sessions"
        sdir.mkdir(parents=True)
        (sdir / "s1.jsonl").write_text('{"role": "user", "content": "do X"}\n')
        return sess

    res = runner.run_scenario(
        sc, port=8999, judge_mode="manual", session_factory=factory, judge_fn=lambda *a, **k: None
    )
    bundle = Path(res.report_path).parent
    assert (bundle / "settings.json").read_text().strip()  # populated, not the old ""
    assert '"content": "do X"' in (bundle / "session.jsonl").read_text()


def test_model_override_injected_into_settings_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A --model override must reach the settings scope on the field that matches the active
    # provider (anthropic_model in cloud mode, llm_model locally), so the run uses that model
    # and the byte-exact restore afterward leaves the user's config untouched.
    import contextlib
    from collections.abc import Iterator

    captured: dict[str, object] = {}

    @contextlib.contextmanager
    def _fake_scope(updates: dict[str, object], **_: object) -> Iterator[None]:
        captured.update(updates)
        yield

    monkeypatch.setattr(runner, "settings_scope", _fake_scope)
    sc = Scenario(
        name="t",
        autonomy="auto",
        strategy="unattended",
        task="do X",
        success_criteria="did X",
        checks=(FileExists("hello.py"),),
    )
    sess = _FakeSession(["❯ ", "⏺ done\n❯ "])

    def factory(argv, cwd):  # type: ignore[no-untyped-def]
        (Path(cwd) / "hello.py").write_text("hi")
        return sess

    res = runner.run_scenario(
        sc,
        port=8999,
        judge_mode="manual",
        model="claude-sonnet-4-5",
        session_factory=factory,
        judge_fn=lambda *a, **k: None,
    )
    assert res.passed is True
    # Landed on whichever model field the active provider uses.
    assert "claude-sonnet-4-5" in (captured.get("anthropic_model"), captured.get("llm_model"))


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
