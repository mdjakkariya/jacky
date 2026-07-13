"""Tests for the code execution tool (run_command) — runner injected, no real process."""

from __future__ import annotations

from pathlib import Path

from autobot.tools.access import AccessBroker, AccessPolicy
from autobot.tools.code.shell import CommandRunner, run_command


class _FakeConfirmer:
    def __init__(self, grant: bool) -> None:
        self._grant = grant

    def confirm(self, prompt: str, kind: str = "danger") -> bool:
        return self._grant

    def choose(
        self, prompt: str, options: list[dict[str, str]], kind: str = "read", default: str = "read"
    ) -> str:
        return default if self._grant else ""


def _broker(tmp_path: Path, *, grant: bool = True) -> AccessBroker:
    pol = AccessPolicy(store_path=tmp_path / "access.json", workspace_root=tmp_path / "ws")
    return AccessBroker(pol, _FakeConfirmer(grant))


def _fake_runner(rc: int, out: str, timed_out: bool = False) -> CommandRunner:
    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        return rc, out, timed_out

    return run


def test_run_command_success(tmp_path: Path) -> None:
    out = run_command("echo hi", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, "hi\n"))
    assert "hi" in out
    assert "ok" in out.lower()


def test_run_command_nonzero_exit_shows_status(tmp_path: Path) -> None:
    out = run_command("false", _broker(tmp_path), str(tmp_path), runner=_fake_runner(1, "boom\n"))
    assert "exit 1" in out
    assert "boom" in out


def test_run_command_timeout(tmp_path: Path) -> None:
    out = run_command(
        "sleep 999", _broker(tmp_path), str(tmp_path), runner=_fake_runner(124, "partial", True)
    )
    assert "timed out" in out.lower()
    assert "partial" in out


def test_run_command_output_is_capped(tmp_path: Path) -> None:
    big = "x" * 50_000
    out = run_command("gen", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, big))
    assert "truncated" in out.lower()
    assert len(out) < 40_000


def test_run_command_empty(tmp_path: Path) -> None:
    out = run_command("   ", _broker(tmp_path), str(tmp_path), runner=_fake_runner(0, ""))
    assert "command" in out.lower()


def test_run_command_denied_when_not_granted(tmp_path: Path) -> None:
    out = run_command(
        "echo hi", _broker(tmp_path, grant=False), str(tmp_path), runner=_fake_runner(0, "hi\n")
    )
    assert "don't have access" in out.lower()


def test_run_command_timeout_is_clamped(tmp_path: Path) -> None:
    # A caller asking for 10_000s must be clamped to the max; the runner sees the clamp.
    seen: list[float] = []

    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        seen.append(timeout)
        return 0, "ok", False

    run_command("echo hi", _broker(tmp_path), str(tmp_path), timeout=10_000.0, runner=run)
    assert seen == [600.0]


def test_run_command_blocks_dangerous_command_without_running_it(tmp_path: Path) -> None:
    calls: list[str] = []

    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        calls.append(command)
        return 0, "should not run", False

    out = run_command("rm -rf /", _broker(tmp_path), str(tmp_path), runner=run)
    assert "blocked" in out.lower()
    assert calls == []  # the runner was never invoked


def test_run_command_runs_normally_with_empty_allow_and_blocklists(tmp_path: Path) -> None:
    out = run_command(
        "echo hi",
        _broker(tmp_path),
        str(tmp_path),
        runner=_fake_runner(0, "hi\n"),
        allowlist=[],
        blocklist=[],
    )
    assert "hi" in out
    assert "ok" in out.lower()


def test_run_command_blocks_command_matching_user_blocklist(tmp_path: Path) -> None:
    calls: list[str] = []

    def run(command: str, cwd: str, timeout: float) -> tuple[int, str, bool]:
        calls.append(command)
        return 0, "should not run", False

    out = run_command(
        "npm publish",
        _broker(tmp_path),
        str(tmp_path),
        runner=run,
        blocklist=["npm publish"],
    )
    assert "blocked" in out.lower()
    assert calls == []
