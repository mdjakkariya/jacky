"""Drive one scenario end-to-end and score it; run the corpus.

Flow: isolate settings (autonomy) → throwaway workspace → spawn the real TUI (test port) →
drive (scripted or unattended) → capture the bundle → deterministic checks → judge → teardown.
The PTY session and the judge are injected so this orchestration unit-tests with fakes; the
real run is dogfooded via ``make e2e``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autobot.config import Settings
from autobot.e2e import markers
from autobot.e2e.artifact import RunRecord, write_bundle
from autobot.e2e.judge import judge_auto, run_checks
from autobot.e2e.pty_session import PtySession, jack_argv
from autobot.e2e.scenario import ApprovePlan, Confirm, Expect, Key, Scenario, Send
from autobot.e2e.settings_scope import settings_scope
from autobot.e2e.workspace import workspace
from autobot.logging_setup import get_logger

_log = get_logger("e2e")

_E2E_ROOT = "~/.autobot/e2e"
_STARTUP_TIMEOUT = 30.0

SessionFactory = Callable[[list[str], str], Any]
JudgeFn = Callable[..., dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of one scenario run."""

    name: str
    passed: bool
    report_path: str
    verdict: dict[str, Any] | None


def _approve(session: Any, log: list[dict[str, Any]]) -> None:
    session.send_key("1")
    session.send_key("enter")
    log.append({"action": "approve"})


def drive_scripted(session: Any, sc: Scenario, *, log: list[dict[str, Any]]) -> None:
    """Run the scenario's explicit steps in order."""
    for step in sc.steps:
        if isinstance(step, Send):
            session.send(step.text)
            log.append({"action": "send", "text": step.text})
        elif isinstance(step, Key):
            session.send_key(step.name)
            log.append({"action": "key", "name": step.name})
        elif isinstance(step, Expect):
            ok = session.wait_for(markers.BY_NAME[step.marker], step.timeout)
            log.append({"action": "expect", "marker": step.marker, "ok": ok})
            if not ok:
                raise TimeoutError(f"marker {step.marker!r} not seen in {step.timeout}s")
        elif isinstance(step, (ApprovePlan, Confirm)):
            marker = "plan_card" if isinstance(step, ApprovePlan) else "permission_card"
            if not session.wait_for(markers.BY_NAME[marker], 90.0):
                raise TimeoutError(f"{marker} never appeared")
            _approve(session, log)


def drive_unattended(
    session: Any, sc: Scenario, *, log: list[dict[str, Any]], turn_timeout: float = 180.0
) -> None:
    """Send the task, auto-approve any gate that appears, wait until idle."""
    session.send(sc.task)
    log.append({"action": "send", "text": sc.task})
    # Wait for the turn to actually start before treating idle as "done" (avoid matching
    # the stale pre-send idle screen).
    session.wait_for(lambda s: not markers.idle_prompt(s), turn_timeout)
    for _ in range(50):  # bounded: at most 50 gate approvals
        if not session.wait_for(
            lambda s: markers.idle_prompt(s) or markers.any_gate(s), turn_timeout
        ):
            raise TimeoutError("turn did not reach idle or a gate")
        if not markers.any_gate(session.screen_text()):
            return  # idle → done
        _approve(session, log)
        # Let the just-approved gate clear before looping, so we don't re-approve it.
        session.wait_for(lambda s: not markers.any_gate(s), turn_timeout)
    raise TimeoutError("too many gate prompts without completing")


def run_scenario(
    sc: Scenario,
    *,
    port: int,
    judge_mode: str,
    judge_model: str | None = None,
    keep: bool = False,
    session_factory: SessionFactory = lambda argv, cwd: PtySession.spawn(argv, cwd),
    judge_fn: JudgeFn = judge_auto,
) -> Result:
    """Run one scenario and return its scored result."""
    sc.validate()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    settings = Settings.load()
    provider = f"{settings.llm_provider}:{settings.llm_model}"
    log: list[dict[str, Any]] = []
    screen, raw = "", b""
    with settings_scope({"coding_autonomy": sc.autonomy}):  # noqa: SIM117 (wraps the workspace)
        with workspace(sc.seed_files, keep=keep) as ws:
            session = session_factory(jack_argv(port), str(ws))
            try:
                session.wait_for(markers.idle_prompt, _STARTUP_TIMEOUT)
                if sc.strategy == "scripted":
                    drive_scripted(session, sc, log=log)
                else:
                    drive_unattended(session, sc, log=log)
            except TimeoutError as exc:
                log.append({"action": "timeout", "error": str(exc)})
                _log.warning("scenario timed out name=%s: %s", sc.name, exc)
            finally:
                screen, raw = session.screen_text(), session.raw_bytes()
                session.close()
            checks = run_checks(list(sc.checks), ws, screen)
            checks_pass = all(c["ok"] for c in checks)
            verdict: dict[str, Any] | None = None
            if judge_mode == "auto":
                verdict = judge_fn(
                    sc.name, sc.task, sc.success_criteria, screen, checks, judge_model=judge_model
                )
            record = RunRecord(
                name=sc.name,
                task=sc.task,
                criteria=sc.success_criteria,
                autonomy=sc.autonomy,
                strategy=sc.strategy,
                provider=provider,
                screen=screen,
                raw=raw,
                steps_log=log,
                checks=checks,
                verdict=verdict,
                settings_snapshot="",
            )
            root = Path(_E2E_ROOT).expanduser() / f"{stamp}-{sc.name}"
            bundle = write_bundle(record, root=str(root))
    judged_ok = True if verdict is None else bool(verdict.get("pass"))
    passed = checks_pass and judged_ok
    _log.info("scenario done name=%s passed=%s bundle=%s", sc.name, passed, bundle)
    return Result(sc.name, passed, str(bundle / "report.md"), verdict)


def run_suite(scenarios: list[Scenario], **kw: Any) -> list[Result]:
    """Run each scenario in order; return the results (a scoreboard)."""
    results = []
    for sc in scenarios:
        results.append(run_scenario(sc, **kw))
    return results
