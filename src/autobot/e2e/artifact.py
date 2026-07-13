"""Assemble the per-run artifact bundle — the one path you hand an LLM to debug a run.

Everything (screen, logs, transcript, settings) is redacted through the assistant's
diagnostics redactor, so a bundle is safe to paste anywhere. ``report.md`` stitches the
key data + relative paths into a single readable file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from autobot.diagnostics import redact
from autobot.logging_setup import get_logger

_log = get_logger("e2e")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything captured from one scenario run, pre-bundle."""

    name: str
    task: str
    criteria: str
    autonomy: str
    strategy: str
    provider: str
    screen: str
    raw: bytes
    steps_log: list[dict[str, object]] = field(default_factory=list)
    checks: list[dict[str, object]] = field(default_factory=list)
    verdict: dict[str, object] | None = None
    daemon_log: str = ""
    session_jsonl: str = ""
    settings_snapshot: str = ""


def _report_md(r: RunRecord) -> str:
    checks = "\n".join(f"- {'✅' if c.get('ok') else '❌'} {c}" for c in r.checks) or "- (none)"
    verdict_str = (
        json.dumps(r.verdict, indent=2)
        if r.verdict is not None
        else "manual — verify from this bundle"
    )
    return redact(
        f"# E2E run — {r.name}\n\n"
        f"- task: {r.task}\n- criteria: {r.criteria}\n"
        f"- autonomy: {r.autonomy} · strategy: {r.strategy} · provider: {r.provider}\n\n"
        f"## Deterministic checks\n{checks}\n\n"
        f"## Verdict\n```json\n{verdict_str}\n```\n\n"
        f"## Final screen\n```\n{r.screen}\n```\n\n"
        f"## Files\nscreen.txt · raw.ansi · steps.jsonl · daemon.log · session.jsonl · "
        f"settings.json{' · judge.json' if r.verdict is not None else ''}\n"
    )


def write_bundle(record: RunRecord, *, root: str) -> Path:
    """Write the bundle dir under ``root``; return its path."""
    d = Path(root).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(_report_md(record), encoding="utf-8")
    (d / "screen.txt").write_text(redact(record.screen), encoding="utf-8")
    (d / "raw.ansi").write_text(redact(record.raw.decode("utf-8", "replace")), encoding="utf-8")
    (d / "steps.jsonl").write_text(
        redact("\n".join(json.dumps(s) for s in record.steps_log)), encoding="utf-8"
    )
    (d / "daemon.log").write_text(redact(record.daemon_log), encoding="utf-8")
    (d / "session.jsonl").write_text(redact(record.session_jsonl), encoding="utf-8")
    (d / "settings.json").write_text(redact(record.settings_snapshot), encoding="utf-8")
    manifest = {
        "name": record.name,
        "task": record.task,
        "autonomy": record.autonomy,
        "strategy": record.strategy,
        "provider": record.provider,
        "checks": record.checks,
        "verdict": record.verdict,
    }
    (d / "manifest.json").write_text(redact(json.dumps(manifest, indent=2)), encoding="utf-8")
    if record.verdict is not None:
        (d / "judge.json").write_text(
            redact(json.dumps(record.verdict, indent=2)), encoding="utf-8"
        )
    _log.info("bundle written path=%s verdict=%s", d, record.verdict)
    return d
